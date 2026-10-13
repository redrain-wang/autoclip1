"""Source-bound character observations and per-line attribution, without a script."""

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .util import digest, read_json, source_identity, write_json

VERSION = 'speakers-2'
GROUNDING_VERSION = 3


class FaceFrame(BaseModel):
    id: int
    visible: list[str]
    uncertain: bool


class FaceMap(BaseModel):
    frames: list[FaceFrame]


class SpeakerLine(BaseModel):
    evidence_id: int
    speaker: str
    basis_type: Literal['address_response', 'named_before_or_after', 'visual_dialogue_context', 'unknown']
    context_ids: list[int]
    frame_ids: list[int]
    reaction_frame_ids: list[int] = Field(default_factory=list)
    basis: str


class SpeakerMap(BaseModel):
    lines: list[SpeakerLine]


def observation_times(lines, start, end):
    """Look around utterances, rather than uniformly sampling a long story."""
    values = {round(t, 3) for s in lines for t in (s.start+.15, (s.start+s.end)/2, s.end-.15)
              if start <= t < end}
    values.update((round(start, 3), round(max(start, end-.1), 3)))
    times = sorted(values)
    if len(times) > 48:
        times = [times[round(i*(len(times)-1)/47)] for i in range(48)]
    return times


def ground_speakers(raw, lines, target_ids, frames, times, anchors):
    data = SpeakerMap.model_validate(raw)
    lookup = {s.id: s for s in lines}
    face_lookup = {f['id']: f for f in frames}
    refs = {f'R{i}': a for i, a in enumerate(anchors)}
    received = [x.evidence_id for x in data.lines]
    if len(received) != len(set(received)) or set(received) != set(target_ids):
        raise ValueError('发言整理需逐条覆盖目标字幕，不能合并问话者与回应者')
    # ASR splits a continuous speech into many subtitles. Reuse its cited
    # observations until an actual speaker change, gap, or missing link.
    groups = []
    positions = {s.id: i for i, s in enumerate(sorted(lines, key=lambda s: s.start))}
    for row in sorted(data.lines, key=lambda r: lookup[r.evidence_id].start):
        previous = groups[-1][-1] if groups else None
        linked = previous and (previous.evidence_id in row.context_ids or row.evidence_id in previous.context_ids)
        if (linked and row.speaker == previous.speaker and row.speaker in refs
                and row.basis_type != 'unknown' and previous.basis_type != 'unknown'
                and positions[row.evidence_id] == positions[previous.evidence_id]+1
                and lookup[row.evidence_id].start-lookup[previous.evidence_id].end <= 3):
            groups[-1].append(row)
        else:
            groups.append([row])
    utterances = {r.evidence_id: group for group in groups for r in group}
    turns = []
    for row in data.lines:
        line = lookup[row.evidence_id]
        problems = []
        if row.speaker not in refs or row.basis_type == 'unknown':
            problems.append('人物尚未落实')
        if not row.context_ids or any(i not in lookup for i in row.context_ids):
            problems.append('缺少有效的相邻对话依据')
        elif any(lookup[i].end < line.start-35 or lookup[i].start > line.end+35 for i in row.context_ids):
            problems.append('身份依据不在附近问答中')
        if not row.basis.strip():
            problems.append('缺少归属说明')
        all_frames = row.frame_ids + row.reaction_frame_ids
        if any(f not in face_lookup for f in all_frames):
            problems.append('引用不存在的画面')
        if set(row.frame_ids) & set(row.reaction_frame_ids):
            problems.append('同一画面不能同时证明发言者又被认作听话反应')
        group = utterances[row.evidence_id]
        group_start = min(lookup[r.evidence_id].start for r in group)
        group_end = max(lookup[r.evidence_id].end for r in group)
        cited_frames = {f for r in group for f in r.frame_ids}
        reaction_frames = {f for r in group for f in r.reaction_frame_ids}
        supporting = sorted(f for f in cited_frames-reaction_frames if f in face_lookup
                      and group_start-8 <= times[f] <= group_end+8
                      and row.speaker in face_lookup[f]['visible'] and not face_lookup[f]['uncertain'])
        if not supporting:
            problems.append('附近画面没有落实该人物')
        if row.basis_type == 'visual_dialogue_context' and len(supporting) < 2:
            problems.append('单张同期人物画面无法区分发言与听话')
        grounded_basis = row.basis_type
        if row.basis_type in {'address_response', 'named_before_or_after'} and row.speaker in refs:
            names = [refs[row.speaker]['name'], *refs[row.speaker]['aliases']]
            texts = ' '.join(lookup[i].text for i in row.context_ids if i in lookup)
            # 皇阿玛 names the addressee, while 儿臣 is shared by several people.
            names = [n for n in names if n not in {'儿臣', '臣', '皇阿玛', '皇上', '皇帝'}]
            if len(set(row.context_ids)) < 2 or not any(n in texts for n in names):
                if len(supporting) >= 2:
                    # The observation may be usable even if the model picked
                    # the wrong evidence category. Do not fabricate a name.
                    grounded_basis = 'visual_dialogue_context'
                else:
                    problems.append('所称点名与回应缺少对应姓名及连续证据')
        turns.append({'evidence_ids': [row.evidence_id], 'speaker': row.speaker,
                      'basis_type': grounded_basis, 'claimed_basis_type': row.basis_type,
                      'context_ids': row.context_ids, 'utterance_ids': [r.evidence_id for r in group],
                      'frame_ids': supporting, 'cited_frame_ids': row.frame_ids,
                      'reaction_frame_ids': row.reaction_frame_ids,
                      'basis': row.basis, 'anchor_id': refs[row.speaker]['id'] if not problems else None,
                      'status': 'model_supported' if not problems else 'unresolved', 'issues': problems})
    return turns


def _window(video, folder, anchors, images, lines, target_ids, times, cfg, llm):
    from PIL import Image, ImageDraw
    from .identity import reference_board
    from .ingest import contact_sheet
    from .director import compact
    signature = digest({'version': VERSION, 'source': source_identity(video), 'anchors': anchors,
                        'lines': [s.model_dump() for s in lines], 'targets': target_ids, 'times': times,
                        'model': cfg.llm_model, 'endpoint': cfg.llm_base_url})
    folder = folder/signature
    folder.mkdir(parents=True, exist_ok=True)
    sheets = []
    for offset in range(0, len(times), 8):
        source = contact_sheet(video, folder, times[offset:offset+8], f'scene-{offset}')
        target = source.with_name(source.stem+'-ids.jpg')
        if not target.exists():
            with Image.open(source) as sheet:
                draw = ImageDraw.Draw(sheet)
                for j in range(min(8, len(times)-offset)):
                    x,y=(j%2)*480,(j//2)*294
                    draw.rectangle((x,y,x+480,y+23),fill='#111111')
                    draw.text((x+5,y+5),f'F{offset+j} SOURCE {times[offset+j]:.3f}s',fill='white')
                sheet.save(target,quality=90)
        sheets.append(target)
    reference = [reference_board(anchors, images, folder)] if images else []
    face_path = folder/'faces.json'
    if face_path.exists():
        faces = FaceMap.model_validate(read_json(face_path)).model_dump()['frames']
    else:
        faces = FaceMap.model_validate(llm.json('你是影视人物画面对照员，只比较所给参考照片和当前照片，没有台词归属任务。',
            '先只整理F编号画面中能与R编号参考确定对应的脸。参考中同一R编号的不同照片是同一个人。'
            '不根据衣服、官帽、画面字幕的姓名或你对本剧的记忆认人；仔细比较脸型、眉眼、鼻子和胡须。'
            '参考库没有这个人就visible=[]，不选最像者凑数。遮挡、背影、模糊不认。'
            '同一张脸在相邻画面里应保持一致，不能因为换了角度就忽然认成另一人。'
            '每个F编号必须出现一次，visible填R编号；uncertain指脸部匹配有疑问。'
            f'前{len(reference)}张是R参考，其后是F场景帧，有效F编号0–{len(times)-1}。'
            f'格式{json.dumps(FaceMap.model_json_schema(),ensure_ascii=False)}', reference+sheets)).model_dump()['frames']
        write_json(face_path, {'frames': faces})
    if sorted(f['id'] for f in faces) != list(range(len(times))):
        raise ValueError('场景人物核对遗漏或重复帧编号')
    refs = {f'R{i}' for i in range(len(anchors))}
    if any(r not in refs for f in faces for r in f['visible']):
        raise ValueError('画面引用未知参考编号')
    path = folder/'attributions.json'
    if path.exists():
        raw = read_json(path)
    else:
        names = {f'R{i}': {'name': a['name'], 'aliases': a['aliases']} for i,a in enumerate(anchors)}
        raw = llm.json('你是原片对话整理员，未提供解说稿；逐条区分发言者、听话者与被提及者。',
            '根据先行的脸部对照、连续问答和镜头前后关系，给每条目标字幕分别指定speaker=R编号或unknown。'
            '脸部对照已落实的角色可以结合一问一答确定身份，不要求每人自报姓名。'
            '例如问者称对方乙，随后乙称问者甲，且甲乙均已匹配到画面，可用前后互称确定问答双方；'
            '同一人连续发问的几条字幕可以共用这组互称依据，不要因为每条没有姓名就全部unknown。'
            '不能把多条问答合成同一发言：点名、回应“在”、再提问，可能是三次换人。'
            '一段话中镜头切到另一人，可能只是在拍听话反应；需要看前后同一段发言和随后谁被点名认可。'
            '说话人可以在同期画面外。frame_ids仅引用支持发言者身份的前后画面；同期听话镜头记入reaction_frame_ids，不能用来认发言者。'
            '静帧不能证明唇音同步，不声称听过声音；但可以据多帧、称呼和对话内容做归属推断。'
            '这些依据合起来仍无法区分角色/发言者才用unknown，不能用文案或常识补全。'
            'context_ids引用附近问答，不能只凭“儿臣”“臣”认皇子；被点名的人不等于发出点名的人。'
            'basis_type: address_response为点名后回应，named_before_or_after为相邻对白明确称呼该发言者；'
            'visual_dialogue_context为多帧与连续问答共同支持；unknown为无法落实。不要为了通过程序编造点名。'
            f'\n人物参考{json.dumps(names,ensure_ascii=False)}\n先行脸部对照{json.dumps(faces,ensure_ascii=False)}'
            f'\n目标字幕ID{target_ids}\n格式{json.dumps(SpeakerMap.model_json_schema(),ensure_ascii=False)}'
            f'\n附近完整问答{compact(lines)}',reference+sheets)
        write_json(path, raw)
    turns = ground_speakers(raw, lines, target_ids, faces, times, anchors)
    result = {'signature':digest([signature,GROUNDING_VERSION]),'grounding_version':GROUNDING_VERSION,
              'times':times,'frames':faces,'turns':turns,
              'sheets':[str(p.resolve()) for p in sheets], 'reference':[str(p.resolve()) for p in reference]}
    write_json(folder/'scene.json',result)
    return result


def recognize_scene(video, root, lines, start, end, cfg, llm, times=None):
    from .identity import load_anchors
    anchors, images = load_anchors(cfg.identity_registry, cfg.series)
    cache_root = (cfg.continuity_history/'人物场景' if cfg.continuity_history else root/'scene-identity')
    targets = [s for s in lines if s.start < end and s.end > start]
    if not targets:
        raise ValueError('场景范围内没有可核对的字幕')
    pieces = []
    # Small adjacent windows keep role changes readable; nearby dialogue remains available.
    for left in range(math.floor(start), math.ceil(end), 90):
        right = min(end, left+90)
        selected = [s for s in targets if left <= max(s.start,start) < right]
        if not selected:
            continue
        nearby = [s for s in lines if s.start < right+12 and s.end > left-12]
        frame_times = ([t for t in times if left-8 <= t <= right+8] if times is not None
                       else observation_times(nearby, max(0,left-8),min(right+8,max(s.end for s in nearby))))
        if not frame_times:
            frame_times = observation_times(selected,left,right)
        pieces.append(_window(video,cache_root,anchors,images,nearby,[s.id for s in selected],frame_times,cfg,llm))
    all_times = sorted({t for p in pieces for t in p['times']})
    frame_map, turns = {}, []
    for piece in pieces:
        remap = {i: all_times.index(t) for i,t in enumerate(piece['times'])}
        for f in piece['frames']:
            key = remap[f['id']]
            existing = frame_map.get(key)
            frame_map[key] = {**f,'id':key}
            if existing and (set(existing['visible']) != set(f['visible']) or existing['uncertain']):
                frame_map[key]['uncertain'] = True
        for t in piece['turns']:
            turns.append({**t,'frame_ids':[remap[f] for f in t['frame_ids'] if f in remap],
                          'cited_frame_ids':[remap[f] for f in t['cited_frame_ids'] if f in remap],
                          'reaction_frame_ids':[remap[f] for f in t['reaction_frame_ids'] if f in remap]})
    for turn in turns:
        remaining = sum(not frame_map[f]['uncertain'] and turn['speaker'] in frame_map[f]['visible']
                        for f in set(turn['frame_ids']))
        required = 2 if turn['basis_type'] == 'visual_dialogue_context' else 1
        if turn['anchor_id'] is not None and remaining < required:
            turn.update(anchor_id=None,status='unresolved')
            turn['issues'].append('相邻窗口对参考身份的匹配存在冲突')
    # The audit consumes these globally-labelled boards, not overlapping window labels.
    from .ingest import contact_sheet
    from PIL import Image, ImageDraw
    combined_key = digest([p['signature'] for p in pieces])
    combined = cache_root/'combined'/combined_key
    global_sheets = []
    for offset in range(0,len(all_times),8):
        source = contact_sheet(video,combined,all_times[offset:offset+8],f'scene-{offset}')
        target = source.with_name(source.stem+'-ids.jpg')
        if not target.exists():
            with Image.open(source) as sheet:
                draw=ImageDraw.Draw(sheet)
                for j in range(min(8,len(all_times)-offset)):
                    x,y=(j%2)*480,(j//2)*294
                    draw.rectangle((x,y,x+480,y+23),fill='#111111')
                    draw.text((x+5,y+5),f'F{offset+j} SOURCE {all_times[offset+j]:.3f}s',fill='white')
                sheet.save(target,quality=90)
        global_sheets.append(str(target.resolve()))
    result={'signature':combined_key,'anchors':anchors,'source':source_identity(video),
            'times':all_times,'frames':[frame_map[i] for i in sorted(frame_map)],'turns':turns,
            'unassigned_ids':[], 'sheets':global_sheets,'reference':pieces[0]['reference'],
            'limitations':'独立静帧比对与逐条问答推断；不是声纹/唇音同步识别，也不代表普遍准确率'}
    write_json(root/'scene-identity-latest.json',result)
    return result
