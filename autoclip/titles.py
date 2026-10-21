"""Editorial title policy; candidate selection is a draft, never approval."""
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from .util import read_json

TITLE_POLICY = 'story-title-1'
TITLE_COVER_POLICY = 'title-evidence-cover-1'
TITLE_RULES = (
    '标题须严谨、吸引人、精炼：明确具体人物或处境，突出实际发生的行动、冲突或反差，'
    '优先12–22字，最多24字（含标点），不为凑字删掉必要限定。'
    '使用自然、顺口的中文，不写公文式缩略词，不堆人名、动作和连接词，不把一长串剧情压成病句。'
    '不能只是“事件之后：概念与人物”等目录式标签，例如“废储之后：观望与妖道”；'
    '不能靠震惊、惊天秘密、所有人都等空泛夸张吸引点击。'
    '选择成片真正展开并有交代的核心看点，不以短暂支线冒充全片主题。'
    '长片可聚焦充分展开的一个主要冲突，不必串起所有章节；不能仅因冲突出现在末段就视为次要支线。'
    '疑问句不是必选；用了“为何/为什么”，成片必须呈现可理解的原因，不能凭空设问或暗示无据动机。'
    '标题的每个事实须由实际选用剧情支持，不能借未剪入情节或后集结果。'
    '人物预测、指责、传言必须保留“称/说/被指”等归属，不能写成已经发生的事实。'
)


class TitleCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=2, max_length=24)
    evidence_ids: list[StrictInt] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class TitleSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidates: list[TitleCandidate] = Field(min_length=3, max_length=3)
    selected: StrictInt = Field(ge=0, le=2)
    selection_reason: str = Field(min_length=1)


def title_issues(title):
    if not 2 <= len(title) <= 24 or title != title.strip() or '\n' in title or '\r' in title:
        return [{'id': 'title', 'message': '标题须为2–24字的单行文字，不得有首尾空白；精炼但保留必要事实限定。'}]
    return []


def reviewed_title_evidence(report):
    """Use the approved title's citations to locate relevant retained footage."""
    if report.get('passed') is not True or report.get('title_policy') != TITLE_POLICY:
        raise ValueError('封面选图需要当前标题的核定记录')
    evidence = set()
    for path in report['batches']:
        batch = read_json(Path(path))
        for unit in batch.get('raw', {}).get('units', []):
            if unit.get('id') == 'title' and unit.get('verdict') == 'supported':
                evidence.update(unit['evidence_ids'])
    return sorted(evidence)


def title_repair_prompt(clip, issues, lines, voice_rows):
    return (TITLE_RULES + '仅修标题，不修改稿件、镜头、角度或钩子。'
            '提出3个实质不同的候选，逐个记录剧情证据编号和看点，比较准确性、清晰度、吸引力与精炼程度后选择一个。'
            '不要只换同义词。原稿、angle、hook及审核意见不是事实依据；ASR可能错字，以所附原片字幕图为准。'
            '声音候选不是确定身份；用明确称呼和上下文核对。候选仍需随后独立审核，不能自批通过。'
            f'\n格式={json.dumps(TitleSelection.model_json_schema(), ensure_ascii=False)}'
            f'\n当前成片={clip.model_dump_json()}'
            f'\n问题={json.dumps(issues, ensure_ascii=False)}'
            f'\n选用对白={json.dumps(lines, ensure_ascii=False)}'
            f'\n声音候选={json.dumps(voice_rows, ensure_ascii=False)}')


def title_changes(raw, eligible):
    selection = TitleSelection.model_validate(raw)
    titles = [c.title.strip() for c in selection.candidates]
    if len(set(titles)) != 3:
        raise ValueError('标题候选不能重复')
    for candidate in selection.candidates:
        if title_issues(candidate.title):
            raise ValueError('标题必须为无首尾空白的单行文字')
        if not set(candidate.evidence_ids).issubset(eligible):
            raise ValueError('标题候选引用了成片未选用的证据')
    return {'changes': [{'id': 'title', 'text': titles[selection.selected]}]}
