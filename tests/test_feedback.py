import csv
import pytest
from autoclip.feedback import summarize_project
from autoclip.util import write_json


def test_feedback_requires_exact_delivered_version_and_valid_rates(tmp_path):
    episode=tmp_path/'episodes/01'
    write_json(episode/'status.json',{'stage':'已完成'})
    write_json(episode/'results.json',[{'production_id':'v1','title':'第一集','duration':120,'video':'/local/video.mp4'}])
    path=tmp_path/'input.csv'
    def feedback(identity,rate):
        with path.open('w',newline='') as f:
            writer=csv.writer(f);writer.writerow(['production_id','platform','published_at','snapshot_at','views','completion_rate'])
            writer.writerow([identity,'test','2026-10-17','2026-10-18',100,rate])
    feedback('v1',.5)
    result=summarize_project(tmp_path,path)
    assert result['platform_observations'][0]['completion_rate']==.5
    feedback('other',.5)
    with pytest.raises(ValueError,match='未关联'):summarize_project(tmp_path,path)
    feedback('v1',1.5)
    with pytest.raises(ValueError,match='完播率'):summarize_project(tmp_path,path)
