"""Local production metrics and optional exported platform observations."""
import csv
import math
from pathlib import Path
from .util import read_json, write_json


def summarize_project(root, feedback=None):
    root = Path(root)
    episodes = read_json(root/'series-status.json').get('episodes',[]) if (root/'series-status.json').exists() else []
    clips = {}
    for marker in (root/'episodes').glob('*/results.json'):
        status = marker.parent/'status.json'
        if not status.exists() or read_json(status).get('stage') != '已完成':
            continue
        for clip in read_json(marker):
            if clip.get('production_id'):
                clips[clip['production_id']] = clip
    observations = []
    if feedback:
        with Path(feedback).open(encoding='utf-8-sig',newline='') as stream:
            reader = csv.DictReader(stream)
            required = {'production_id','platform','published_at','snapshot_at','views','completion_rate'}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError('反馈CSV需包含：'+', '.join(sorted(required)))
            seen = set()
            for row in reader:
                key = (row['production_id'],row['platform'],row['published_at'],row['snapshot_at'])
                if key in seen or any(not field.strip() for field in key):
                    raise ValueError('反馈记录重复或缺少版本/平台/时间')
                seen.add(key)
                if row['production_id'] not in clips:
                    raise ValueError('反馈未关联当前项目已完成的成片版本')
                views,rate = int(row['views']),float(row['completion_rate'])
                if views<0 or not math.isfinite(rate) or not 0<=rate<=1:
                    raise ValueError('播放量须非负，完播率须为0–1')
                clip = clips[row['production_id']]
                observations.append({**row,'views':views,'completion_rate':rate,
                                     'duration':clip['duration'],'title':clip['title']})
    if feedback:
        write_json(root/'platform-feedback.json',observations)
    elif (root/'platform-feedback.json').exists():
        observations = read_json(root/'platform-feedback.json')
    result = {'episodes':episodes,'deliverables':[{k:c[k] for k in ('production_id','title','duration','video')} for c in clips.values()],
              'platform_observations':observations,'interpretation':'模型评分不等于观看效果；不同平台、时长与发布时间不直接混算。无数据不推断提升。'}
    if (root/'project.json').exists():
        project = read_json(root/'project.json')
        if project.get('purpose'):
            result.update(purpose=project['purpose'],duration_policy=project['duration_policy'])
    write_json(root/'project-report.json',result)
    template = root/'feedback-template.csv'
    with template.open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['production_id','platform','published_at','snapshot_at','views','completion_rate'])
        for identity in clips:
            writer.writerow([identity,'','','','',''])
    return result
