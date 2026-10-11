from types import SimpleNamespace

import pytest

from autoclip.models import Line
from autoclip.study import ScriptVersion, assess_depth, audit_carrier, compare_scripts


def script():
    return ScriptVersion(title="一两银子的问话", question="已还清为什么仍问话", paragraphs=[
        {"text": text, "evidence_ids": [i+1], "interpretation": "备注中的分析不能给口播加分", "alternative": ""}
        for i, text in enumerate(["已还清只是账面问题，问话却指向借款的原因。", "少借降低还款压力，也留下是否跟随众人的问题。",
                                  "这是问话者的解释，原片没有确认当事人的真实内心。", "问话最后收下银子，结束了这次争论。"])])


def test_depth_cannot_pass_using_notes_or_duplicate_paragraphs():
    good = {"causal": [{"paragraph": i+1, "excerpt": p.text, "explanation": "讲明选择和代价"}
                        for i, p in enumerate(script().paragraphs[:2])],
            "limitation": [{"paragraph": 3, "excerpt": script().paragraphs[2].text, "explanation": "区分推测与事实"}], "issues": []}
    assert assess_depth(good, script())["passed"]
    good["causal"][1]["excerpt"] = script().paragraphs[1].interpretation
    assert not assess_depth(good, script())["passed"]
    good["causal"][1] = good["causal"][0]
    assert not assess_depth(good, script())["passed"]


def test_audit_carrier_refuses_fabricated_subtitle_ids():
    with pytest.raises(ValueError, match="区间外"):
        audit_carrier(script(), {1: Line(id=1, start=10, end=13, text="对白")})


@pytest.mark.parametrize("repair_budget,expected", [(0, 1), (1, 2), (5, 3)])
def test_repair_is_bounded_preserves_failed_attempts_and_never_publishes(tmp_path, monkeypatch, repair_budget, expected):
    import autoclip.identity as identity
    import autoclip.study as study
    from autoclip.util import read_json
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fixture")
    lines = [Line(id=i, start=i*10, end=i*10+3, text="对白") for i in range(1, 5)]
    monkeypatch.setattr(identity, "scene_identity", lambda *args: {"anchors": [], "turns": []})
    monkeypatch.setattr(identity, "audit_identity", lambda *args: {"passed": False, "issues": [{"message": "身份待核实"}]})
    monkeypatch.setattr(study, "review_depth", lambda *args: {"passed": True, "issues": []})

    class Model:
        calls = 0
        def json(self, *args):
            self.calls += 1
            return {"current": script().model_dump(), "analysis": script().model_dump()} if self.calls == 1 else script().model_dump()

    llm = Model()
    cfg = SimpleNamespace(series="测试", repair_attempts=repair_budget)
    result = compare_scripts(video, tmp_path / "run", lines, 0, 50, cfg, llm)
    for name, item in result["versions"].items():
        assert not item["passed"] and not item["renderable"]
        assert len(item["attempts"]) == expected
        assert all(not a["identity_passed"] for a in item["attempts"])
        assert read_json(tmp_path / "run" / name / "attempt-0/result.json")["issues"]
    assert llm.calls == 1 + 2*(expected-1)
    assert not list((tmp_path / "run").rglob("*.mp4"))
