from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Line(BaseModel):
    id: int
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str
    confidence: float = 1.0

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("subtitle has invalid duration")
        return self


class Beat(BaseModel):
    mode: Literal["narration", "original"]
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = ""
    evidence_ids: list[int] = Field(min_length=1)
    reason: str

    @model_validator(mode="after")
    def valid(self):
        if not 1 <= self.end - self.start <= 40:
            raise ValueError(f"原片镜头{self.start:.2f}–{self.end:.2f}仅{self.end-self.start:.2f}秒，需要1–40秒；口播需留6–22秒画面")
        if self.mode == "narration" and not self.text.strip():
            raise ValueError("narration text required")
        if self.mode == "original" and self.text.strip():
            raise ValueError("original audio must not contain invented dialogue text")
        return self


class Clip(BaseModel):
    title: str = Field(min_length=2, max_length=32)
    angle: str
    hook: str
    beats: list[Beat] = Field(min_length=4, max_length=96)


class Plan(BaseModel):
    episode_summary: str
    genre: str
    style: str
    clips: list[Clip] = Field(min_length=0, max_length=10)


def evidence_matches(line, beat, context):
    overlap = line.start < beat.end and line.end > beat.start
    preceding = beat.mode == "narration" and 0 <= beat.start-line.end <= 15
    recap = beat.mode == "narration" and line.id in context and line.end <= beat.start
    return overlap or preceding or recap


def validate_plan(plan: Plan, lines: list[Line], source_duration: float, count: int, target: int,
                  min_seconds: float | None = None, max_seconds: float | None = None,
                  opening: str = "narration", *, check_duration: bool = True):
    errors = []
    lookup = {s.id: s for s in lines}
    if count == 0 and len(plan.clips) > 6:
        errors.append("自动选题模式每集最多6条，避免重复和低质量凑数")
    elif count != 0 and len(plan.clips) != count:
        errors.append(f"需要 {count} 条视频")
    for i, clip in enumerate(plan.clips):
        if target <= 240 and len(clip.beats) > 24:
            errors.append(f"clips[{i}] 短片最多24个镜头；加长解说需使用长片配置")
        previous_end = -1.0
        estimated = 0
        original = 0
        context = set()
        for j, beat in enumerate(clip.beats):
            label = f"clips[{i}].beats[{j}]"
            if beat.end > source_duration:
                errors.append(f"{label} 越过原片结尾")
            # Explicit long-form teaser: only the first original shot can move
            # ahead of the story. The main timeline remains chronological.
            teaser = opening == "original_teaser"
            if beat.start < previous_end - 0.05 and not (teaser and j == 1):
                errors.append(f"{label} 必须按原片时间递增，不能重复或倒置剧情")
            if teaser and j > 0 and max(beat.start,clip.beats[0].start) < min(beat.end,clip.beats[0].end)-.04:
                errors.append(f"{label} 正文不能重复播放前置高光区间")
            previous_end = beat.end
            def eligible(line):
                # The first bridge may discuss only the teaser viewers have
                # already seen. Later body shots cannot borrow unseen future.
                return (evidence_matches(line,beat,context)
                        or (teaser and j==1 and beat.mode=='narration' and line.id in context))
            if any(s not in lookup for s in beat.evidence_ids):
                errors.append(f"{label} 引用了不存在的字幕 ID")
            elif not any(eligible(lookup[s]) for s in beat.evidence_ids):
                cited = ", ".join(f"[{s}] {lookup[s].start:.2f}–{lookup[s].end:.2f}" for s in beat.evidence_ids)
                errors.append(f"{label} 没有落在画面区间内的字幕证据：画面{beat.start:.2f}–{beat.end:.2f}秒，引用{cited}；仅可引用当前或已展示的事实，不能预借后文对白")
            elif not any(lookup[s].confidence >= .4 and eligible(lookup[s]) for s in beat.evidence_ids):
                errors.append(f"{label} 只有低置信度识别，不能据此写稿")
            context.update(s for s in beat.evidence_ids if s in lookup and lookup[s].start < beat.end and lookup[s].end > beat.start)
            if beat.mode == "original":
                original += beat.end - beat.start
                estimated += beat.end - beat.start
            else:
                estimated += max(2, len(beat.text) / 4.5)
        if opening in {"original", "original_teaser"}:
            if clip.beats[0].mode != "original":
                errors.append(f"clips[{i}] 开头应保留精彩原声，之后再接解说")
            else:
                opening_seconds = 0.0
                opening_end = clip.beats[0].start
                for beat in clip.beats:
                    if beat.mode != "original":
                        break
                    if beat.start > opening_end + .04:
                        errors.append(f"clips[{i}] 原声开场必须连续，不能拼接分散片段凑时长")
                    opening_seconds += beat.end-beat.start
                    opening_end = beat.end
                if opening == "original_teaser" and (len(clip.beats)<2 or clip.beats[1].mode != "narration"):
                    errors.append(f"clips[{i}] 前置高光必须是一段连续原声，随后用解说明确接回正文")
                if opening == "original_teaser" and not 18 <= opening_seconds <= 22:
                    errors.append(f"clips[{i}] 前置高光实长{opening_seconds:.2f}秒，需18–22秒并保留完整对白")
                elif opening == "original" and not 10 <= opening_seconds <= 22:
                    errors.append(f"clips[{i}] 原声开场实长{opening_seconds:.2f}秒，需10–22秒；优先10–18秒并保留完整句子")
            if not any(b.mode == "narration" for b in clip.beats):
                errors.append(f"clips[{i}] 原声开场后需要有观点的解说")
        elif clip.beats[0].mode != "narration":
            errors.append(f"clips[{i}] 开头应有解说钩子")
        if original < 5:
            errors.append(f"clips[{i}] 至少保留5秒原声对白")
        # Character-count timing is only a rough estimate; Chinese TTS and
        # punctuation vary considerably. Enforce the tighter limit after actual
        # synthesis rather than rejecting a valid story before measuring it.
        estimate_ceiling = (max_seconds or target * 1.8) * 1.2
        if check_duration and not (min_seconds * .9 if min_seconds else target * .5) <= estimated <= estimate_ceiling:
            minimum, maximum = min_seconds or target*.6, max_seconds or target*1.8
            difference = f"超出上限{estimated-maximum:.2f}秒" if estimated > maximum else f"距下限差{minimum-estimated:.2f}秒"
            errors.append(f"clips[{i}] 预计时长 {estimated:.2f} 秒不在目标范围内：成片要求{minimum:.0f}–{maximum:.0f}秒，{difference}；这是字数估算，草稿允许上限20%估算误差，最终须真实配音实测通过")
    for i, a in enumerate(plan.clips):
        for b in plan.clips[i + 1:]:
            overlap = sum(max(0, min(x.end, y.end) - max(x.start, y.start)) for x in a.beats for y in b.beats)
            if overlap > 0.4 * min(sum(x.end-x.start for x in a.beats), sum(x.end-x.start for x in b.beats)):
                errors.append("不同视频重复使用的原片超过40%，请更换选题")
    if errors:
        raise ValueError("\n".join(errors))
