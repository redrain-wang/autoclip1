"""Bounded draft patches before the independent measured source review."""
import copy
import json
import re

from .editorial import WRITING_RULES
from .models import Beat, evidence_matches, validate_plan
from .repair import RepairNeeded
from .util import read_json, write_json

VERSION = 'draft-repair-3'


def resume_report(root, lines, seconds, cfg, bounds):
    """Prefer an intact timeline over a more recent failed structural rewrite."""
    from .director import compile_plan
    candidates = []
    for path in [*root.glob('attempt-*.json'),*root.glob('attempt-history/*.json')]:
        report = read_json(path).get('repair',{})
        if not report.get('clip'):
            continue
        raw = {'episode_summary':'当前选题','genre':'剧情','style':cfg.style,'clips':[report['clip']]}
        try:
            plan = compile_plan(raw,lines,bounds,fit_narration=False)
            try:
                validate_plan(plan,lines,seconds,1,cfg.target_seconds,cfg.min_seconds,cfg.max_seconds,cfg.opening,check_duration=False)
                score = 0
            except ValueError as exc:
                score = len(str(exc).splitlines())
                if '必须按原片时间递增' in str(exc) or '越过原片结尾' in str(exc):
                    score += 50
            clip = plan.clips[0]
            opening = []
            for beat in clip.beats:
                if beat.mode!='original':break
                opening.append(beat)
            opening_seconds = sum(b.end-b.start for b in opening)
            score += max(0,opening_seconds-22,10-opening_seconds)/10 if cfg.opening=='original' else 0
            estimated = sum(b.end-b.start if b.mode=='original' else max(2,len(b.text)/4.5) for b in clip.beats)
            score += max(0,estimated-(cfg.max_seconds or cfg.target_seconds*1.8)*1.2)/10
            score += max(0,len(clip.beats)-24)*2
            lookup = {s.id:s for s in lines}
            score += .01*sum(any(lookup.get(i) and lookup[i].start>=b.end for i in b.evidence_ids)
                             for b in clip.beats if b.mode=='narration')
        except (ValueError,KeyError,TypeError):
            score = 1000
        candidates.append((score,-path.stat().st_mtime_ns,str(path),report))
    if not candidates:
        return None
    _,_,path,report = min(candidates,key=lambda x:x[:3])
    write_json(root/'draft-resume.json',{'record':path,'draft_score':min(c[0] for c in candidates),
                                        'note':'仅选草稿，不构成内容或人物审核通过'})
    return report


def repair_draft(raw, report, lines, bounds, cfg, llm, root, signature, attempt):
    """Replace one affected window; never let a local fix rewrite other shots."""
    raw = copy.deepcopy(raw)
    if report.get('clip') and len(raw.get('clips', [])) == 1:
        raw['clips'][0] = copy.deepcopy(report['clip'])
    clips = raw.get('clips', [])
    if len(clips) != 1 or not clips[0].get('beats'):
        return None
    beats = clips[0]['beats']
    messages = '\n'.join(i['message'] for i in report.get('issues', []))
    affected = {int(i) for i in re.findall(r'beats\[(\d+)\]', messages)}
    if (affected and all(i.get('code')=='evidence' for i in report.get('issues',[]))
            and all(i<len(beats) and beats[i].get('mode')=='narration' for i in affected)):
        return repair_narrations(raw,affected,lines,cfg,llm,root,signature,attempt)
    opening_repair = ('原声开场' in messages or '开头' in messages) and '必须按原片时间递增' not in messages
    if opening_repair:
        affected = set()
        for i, beat in enumerate(beats):
            affected.add(i)
            if beat.get('mode') == 'narration':
                break
    # A backwards story or a global duration problem needs structural planning,
    # whereas a citation/cut problem must preserve all the unaffected footage.
    structural = '必须按原片时间递增' in messages or not affected
    first = 0 if structural or opening_repair else max(0, min(affected)-1)
    last = max(affected) if opening_repair else (len(beats)-1 if structural else min(len(beats)-1, max(affected)+1))
    window = [bounds[0] if bounds else 0, bounds[1] if bounds else max(s.end for s in lines)]
    if not structural:
        window[0] = max(window[0],beats[first]['start'])
        if opening_repair:
            initial_original = []
            for beat in beats:
                if beat.get('mode')!='original':break
                initial_original.append(beat)
            if sum(b['end']-b['start'] for b in initial_original)<10:
                # Recover a complete preceding phrase for a too-short opening,
                # without reopening the entire earlier story or another scene.
                window[0] = max(bounds[0] if bounds else 0,beats[first]['start']-10)
    if first:
        window[0] = max(window[0], beats[first-1]['end'])
    if last+1 < len(beats):
        window[1] = min(window[1], beats[last+1]['start'])
    # A 1s original plus a 9s bridge cannot possibly accommodate a 10s
    # opening and useful narration. Include the next complete shot instead of
    # repeatedly asking a model to satisfy an impossible local window.
    if opening_repair:
        while window[1]-window[0]<24 and last+1<len(beats):
            last += 1
            window[1] = min(bounds[1] if bounds else max(s.end for s in lines),
                            beats[last+1]['start'] if last+1<len(beats) else beats[last]['end'])
    payload = {'replace_indices':[first,last], 'source_window':window,
               'beat_schema':Beat.model_json_schema(),
               'required_output_seconds':[cfg.min_seconds,cfg.max_seconds],
               'target_seconds':cfg.target_seconds, 'opening':cfg.opening,
               'draft':{**{k:v for k,v in clips[0].items() if k!='beats'},'beats':beats[first:last+1]},
               'diagnostics':{'issues':report.get('issues',[])},
               'source_lines':[s.model_dump() for s in lines if s.start<window[1] and s.end>window[0]]}
    if structural:
        # A broken ordering is not a useful writing template. Supplying the
        # entire failed timeline encourages copying its reverse jump verbatim.
        payload['draft'] = {k:v for k,v in clips[0].items() if k!='beats'}
        payload['diagnostics'] = {'issues':report.get('issues',[])}
        payload['planning_reference'] = {'original_seconds':round(cfg.target_seconds*cfg.original_ratio_target),
                                        'narration_seconds':round(cfg.target_seconds*(1-cfg.original_ratio_target)),
                                        'maximum_beats':24}
    opening_options = []
    if first==0 and cfg.opening=='original':
        speech = [s for s in lines if s.confidence>=.4 and window[0]<=s.start<s.end<=window[1]]
        anchor = window[0] if structural else beats[0]['start']
        seen = set()
        for a in speech:
            for b in speech:
                for start in {a.start,max(window[0],a.start-.08)}:
                    for end in {b.end,min(window[1],b.end+.12)}:
                        if 10<=end-start<=22 and end<=window[1]-6 and (start,end) not in seen:
                            seen.add((start,end))
                            opening_options.append({'start':start,'end':end,
                                'evidence_ids':[s.id for s in speech if start<=s.start and s.end<=end]})
        opening_options.sort(key=lambda o:(abs(o['start']-anchor),abs(o['end']-o['start']-15)))
        opening_options=opening_options[:12]
        payload['opening_options']=opening_options
    prompt = (
        '返回JSON {"beats":[修复后替换窗口的完整镜头]}。只替换replace_indices闭区间，其他镜头由程序保留。'
        '每项必须含mode/start/end/text/evidence_ids/reason；mode只能original或narration，original的text为空，narration必须填旁白。'
        'evidence_ids使用source_lines中真实字幕id整数，不能省略字段，其他约束见beat_schema。'
        '如果提供opening_options，第一段原声必须直接选择其中一项start/end/evidence_ids，随后留足画面写必要旁白，不能将字幕逐句拆成1秒原声来凑开场。'
        '可合并、拆分、删去重复的镜头；必须保留独立故事的前因后果。顺序与时间递增，禁止倒叙。'
        '镜头仅限source_window，原声切在完整字幕句边界。只有窗口从镜头0开始时才重做开场：原声10–22秒连续且随后接解说。'
        '不要预借未来对白：解说可解释已经展示的原声，或者当前画面有证据的动作。'
        '若旁白引用下一段原声的事实，把关键原声保留在先，再安排解释；不要仅换证据ID。'
        '禁止将旁白全部换成原声来回避证据问题。连续original都算开场，分成多段仍不能超过22秒。'
        '开场后首段旁白只交代已经可见的处境，不复述下一段尚未播放的对白。'
        '若draft没有beats，旧时间线已撤回：只依据source_lines按原片先后重建，不能沿用倒叙；最多24个镜头。'
        '选必要完整对白与必要承接，不要求把source_window全部剪入，次要寒暄可省略，不能将整场对白全保留导致四五百秒。'
        '单镜头1–40秒；解说留足真实配音画面，30字通常需约6秒，不能塞进1–3秒。'
        f'reason、剧名和稿件不是事实证据，主语不明不能猜。以约{cfg.target_seconds}秒为参考分配原声和解说，最终按required_output_seconds实测。'
        '\n'+json.dumps(payload,ensure_ascii=False))
    feedback = ''
    for retry in range(cfg.repair_attempts+1):
        proposal = llm.json('你是影视剪辑时间线修复员，只修给定窗口。'+WRITING_RULES,prompt+feedback)
        record = root/'draft-repairs'/signature/f'{attempt}-window-{retry}.json'
        try:
            if not isinstance(proposal,dict) or set(proposal)!={'beats'} or not isinstance(proposal['beats'],list) or not proposal['beats']:
                raise ValueError('局部草稿修复必须仅返回非空beats数组')
            for beat in proposal['beats']:
                if not isinstance(beat,dict) or not {'mode','start','end','evidence_ids','reason'}.issubset(beat):
                    raise ValueError('局部镜头缺少mode/start/end/evidence_ids/reason必需字段')
                if (type(beat['start']) not in (int,float) or type(beat['end']) not in (int,float)
                        or not window[0]-.001<=beat['start']<beat['end']<=window[1]+.001):
                    raise ValueError(f'只返回原片{window[0]:.2f}–{window[1]:.2f}秒内的替换镜头；其他镜头已保留，禁止重发全稿')
                if not 1<=beat['end']-beat['start']<=40:
                    raise ValueError('每个镜头必须1–40秒；长原声须在完整字幕句边界拆分，不能返回整场对白')
            if len(beats)-(last-first+1)+len(proposal['beats'])>24:
                raise ValueError('替换后短片超过24个镜头；合并次要片段，不能复制全稿')
            if first==0 and cfg.opening=='original':
                opening_beats = proposal['beats']
                if (len(opening_beats)<2 or opening_beats[0]['mode']!='original' or opening_beats[1]['mode']!='narration'
                        or not 10<=opening_beats[0]['end']-opening_beats[0]['start']<=22):
                    raise ValueError('第一个镜头须一段10–22秒完整连续原声，第二个镜头必须是有充足画面的旁白；不要拼接多段原声凑开场')
                if opening_options and not any(abs(o['start']-opening_beats[0]['start'])<.001 and abs(o['end']-opening_beats[0]['end'])<.001 for o in opening_options):
                    raise ValueError('第一段原声必须从opening_options原样选一项时间范围，不得另估时间')
            if structural:
                original = sum(b['end']-b['start'] for b in proposal['beats'] if b['mode']=='original')
                estimated = original+sum(max(2,len(b.get('text',''))/4.5) for b in proposal['beats'] if b['mode']=='narration')
                maximum = cfg.max_seconds or cfg.target_seconds*1.8
                if original>=maximum or estimated>maximum*1.2:
                    raise ValueError(f'当前仅原声{original:.2f}秒，含旁白估算{estimated:.2f}秒，实际须{cfg.min_seconds or cfg.target_seconds*.6:.0f}–{maximum:.0f}秒。'
                        f'选择必要完整原声，参考{cfg.target_seconds*cfg.original_ratio_target:.0f}秒；次要往返对白可简洁转述，保留请求、回应与结果，不能照搬全场对白。')
            write_json(record,{'window':[first,last],'proposal':proposal,'status':'valid_window_requires_full_review'})
            break
        except ValueError as exc:
            write_json(record,{'window':[first,last],'proposal':proposal,'error':str(exc)})
            if retry==cfg.repair_attempts:
                raise RepairNeeded({'stage':'draft_window','clip':clips[0],
                    'issues':[{'code':'source_bounds','message':('原声开场' if opening_repair else '时间线')+f'窗口修复未通过：{exc}'}]}) from exc
            feedback = '\n上轮替换窗口无效：'+str(exc)+'\n上轮响应：'+json.dumps(proposal,ensure_ascii=False)
    revised = copy.deepcopy(raw)
    revised['clips'][0]['beats'][first:last+1] = proposal['beats']
    return revised


def repair_narrations(raw, affected, lines, cfg, llm, root, signature, attempt):
    """A citation failure cannot change the original audio or movie structure."""
    lookup = {s.id:s for s in lines}
    context = set()
    slots = []
    allowed = {}
    for index,b in enumerate(raw['clips'][0]['beats']):
        beat = Beat.model_validate(b)
        if index in affected:
            ids = {s.id for s in lines if s.confidence>=.4 and s.start<beat.end and evidence_matches(s,beat,context)}
            allowed[index] = ids
            slots.append({'beat':index,'shot':[beat.start,beat.end],
                          'allowed_evidence_ids':sorted(ids),
                          'allowed_evidence':[lookup[i].model_dump() for i in sorted(ids)],
                          'reference_max_characters':max(5,int((beat.end-beat.start)*4.5)),
                          'instruction':'按已有画面与已展示原声重写必要承接；只能使用这里的证据，不能预借后文，不得仅改ID保留原断言'})
        context.update(i for i in beat.evidence_ids if i in lookup and lookup[i].start<beat.end and lookup[i].end>beat.start)
    timeline = copy.deepcopy(raw['clips'][0])
    for i,b in enumerate(timeline['beats']):
        b.pop('reason',None)
        if i in affected:
            b.update(text='此段待重写，旧稿不是依据',evidence_ids=[])
    revised = copy.deepcopy(raw)
    for slot in slots:
        target = slot['beat']
        prompt = (
            '返回JSON {"changes":[{"beat":索引,"text":"重写后的完整旁白","evidence_ids":[真实依据]}]}。'
            'changes必须恰好一项，只修current_target，禁止回复其他段。只允许beat/text/evidence_ids三个字段。'
            '不能改start/end/mode/reason或原声。旧旁白已撤回，不要试图保留未证实的旧断言。'
            '只依据当前给出的allowed_evidence写必要承接，交代已发生的行动或处境。'
            '未来对白未提供就不能复述，不能使用作品记忆补全。先听到原声才解释它。'
            '没有身份依据就不猜人名或行为主语，不能拿泛称掩盖错误。'
            '保持简洁，参考字数只是画面余量参考，最终重新实测并独立审核。'
            'evidence_ids严格属于本段allowed_evidence_ids，不借其他镜头的证据编号。'
            '\n'+json.dumps({'current_target':target,'slot':slot,'viewer_timeline':timeline},ensure_ascii=False))
        feedback = ''
        for retry in range(cfg.repair_attempts+1):
            result = llm.json('你是影视口播事实修复员，只修当前一段旁白。'+WRITING_RULES,prompt+feedback)
            record = root/'draft-repairs'/signature/f'{attempt}-narration-{target}-{retry}.json'
            try:
                changes = result.get('changes',[]) if isinstance(result,dict) else []
                if (not isinstance(changes,list) or len(changes)!=1 or not isinstance(changes[0],dict)
                        or type(changes[0].get('beat')) is not int or changes[0]['beat']!=target):
                    raise ValueError(f'证据修复必须只返回当前镜头{target}一项changes')
                change = changes[0]
                if (set(change)!={'beat','text','evidence_ids'} or not isinstance(change['text'],str) or not change['text'].strip()
                        or not isinstance(change['evidence_ids'],list) or not change['evidence_ids']):
                    raise ValueError('证据修复修改了未允许字段或未给出文字和引用')
                invalid = [i for i in change['evidence_ids'] if type(i) is not int or i not in allowed[target]]
                if invalid:
                    raise ValueError(f"镜头{target}引用未允许的{invalid}，只能使用{sorted(allowed[target])}；须依据允许证据重写断言，不仅删换ID")
                revised['clips'][0]['beats'][target].update(text=change['text'],evidence_ids=change['evidence_ids'])
                timeline['beats'][target].update(text=change['text'],evidence_ids=change['evidence_ids'])
                write_json(record,{'proposal':result,'status':'valid_patch_requires_full_review'})
                write_json(root/'drafts'/f'{signature}.json',revised)
                break
            except ValueError as exc:
                write_json(record,{'proposal':result,'error':str(exc)})
                if retry==cfg.repair_attempts:
                    raise RepairNeeded({'stage':'draft_evidence','clip':revised['clips'][0],
                        'issues':[{'code':'evidence','beat':target,
                            'message':f'clips[0].beats[{target}] 旁白证据修复未通过：{exc}'}]}) from exc
                feedback = '\n上轮响应无效：'+str(exc)+'\n上轮响应：'+json.dumps(result,ensure_ascii=False)
    return revised
