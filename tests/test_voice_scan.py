from pathlib import Path

import pytest

from autoclip.util import read_json, source_identity, write_json
from autoclip.voice_scan import scan_series, speech_windows, cluster_unknown


def test_speech_windows_preserve_offsets_and_do_not_extend_short_replies():
    assert speech_windows([[0,7000],[10000,10900],[20000,24000]],120,143) == [(120.,123.),(123.,126.),(140.,143.)]
    assert speech_windows([[0,1000]],0,10) == []
    assert speech_windows([[0,5000]],0,4.6) == [(0.,3.),(3.,4.6)]


def test_scan_cli_accepts_one_episode_and_resolves_default_models(tmp_path,monkeypatch):
    from typer.testing import CliRunner
    from autoclip.cli import app
    import autoclip.voice_scan as module
    video=tmp_path/'01.mp4';video.write_bytes(b'video')
    library=tmp_path/'library.json';library.write_text('{}')
    calls=[]
    monkeypatch.setattr(module,'scan_series',lambda *args:calls.append(args))
    result=CliRunner().invoke(app,['voice-scan-series',str(video),'--library',str(library),
                                   '--output',str(tmp_path/'scan')])
    assert result.exit_code==0,result.output
    assert calls[0][0]==[video] and calls[0][3:5]==(None,None)


def test_discovery_excludes_known_opening_short_and_distorted_segments():
    pytest.importorskip('sklearn')
    base={'status':'unknown','duration':3.,'opening_or_credits':False,'rms':.05,'clipped_fraction':0.,
          'embedding':[1.,0.],'score':.2,'source_id':'ep1'}
    samples=[{**base,'id':'valid'}, {**base,'id':'known','status':'suggested'},
             {**base,'id':'opening','opening_or_credits':True},{**base,'id':'short','duration':1.5},
             {**base,'id':'quiet','rms':.0001},{**base,'id':'distorted','clipped_fraction':.05}]
    groups=cluster_unknown(samples)
    assert len(groups)==1 and groups[0]['sample_ids']==['valid']


def test_scan_resumes_completed_episodes_and_keeps_unknown_out_of_standard_bank(tmp_path,monkeypatch):
    import math
    import wave
    np=pytest.importorskip('numpy')
    pytest.importorskip('funasr')
    import autoclip.voice_scan as scan
    model={'model':'test','sha256':'weights'}
    videos=[tmp_path/'01.mp4',tmp_path/'02.mp4']
    for i,video in enumerate(videos):video.write_bytes(f'original-{i}'.encode())
    calls=[]
    monkeypatch.setattr(scan,'load_encoder',lambda path:(lambda audio:calls.append(len(audio)) or [1.,0.],model))
    monkeypatch.setattr(scan,'duration',lambda path:4.6)
    def decode(args):
        with wave.open(str(args[-1]),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            t=np.arange(round(4.6*16000))/16000
            stream.writeframes((np.sin(t*440*math.tau)*5000).astype('<i2').tobytes())
    monkeypatch.setattr(scan,'run',decode)
    class Vad:
        def generate(self,**kwargs):return [{'value':[[0,4600]]}]
    import funasr
    monkeypatch.setattr(funasr,'AutoModel',lambda **kwargs:Vad())
    vad=tmp_path/'vad';vad.mkdir();(vad/'config.yaml').write_text('test');(vad/'model.pt').write_bytes(b'weights')
    library={'series':'test','model':model,'dataset_id':'reviewed','revision':1,'characters':[
        {'name':'甲','samples':[{'source':source_identity(videos[0]),'embedding':[0.,1.]}]}]}
    bank=tmp_path/'bank.json';write_json(bank,library)
    root=tmp_path/'scan'
    result=scan_series(videos,bank,root,tmp_path,vad)
    assert result['completed']==2 and result['statuses']=={'unknown':4}
    assert len(calls)==4
    result2=scan_series(videos,bank,root,tmp_path,vad)
    assert result2['completed']==2 and len(calls)==4
    assert read_json(root/'library.json')==library
    assert read_json(root/'scan-progress.json')['stage']=='complete'
    videos[0].write_bytes(b'changed')
    with pytest.raises(ValueError,match='原片版本'):
        scan_series(videos,bank,root,tmp_path,vad)


def test_import_discovery_preserves_human_records_and_never_promotes_cluster_identity(tmp_path):
    from autoclip.voice_scan import import_discovery
    from autoclip.voice_review import ReviewStore
    video=tmp_path/'01.mp4';video.write_bytes(b'original source')
    source=source_identity(video)
    base={'id':'1'*16,'source_id':'s1','start':3.,'end':6.,'embedding':[1.,0.],'text':'已确认','group_id':'g1'}
    manifest={'dataset_id':'review-batch','series':'test','model':{'sha256':'abc'},
              'sources':[{'id':'s1','label':'01','source':source,'duration':100}],
              'samples':[base],'groups':[{'id':'g1','sample_ids':[base['id']],'preview_ids':[base['id']]}],
              'coverage_targets':[{'id':'a','name':'甲','aliases':[]},{'id':'b','name':'乙','aliases':[]}]}
    review={'dataset_id':'review-batch','revision':1,'decisions':{base['id']:{'status':'named','name':'甲'}}}
    root=tmp_path/'review';write_json(root/'manifest.json',manifest);write_json(root/'review.json',review)
    scan_root=tmp_path/'scan'
    new={**base,'id':'4'*16,'start':40.,'end':43.,'embedding':[0.,1.],'status':'unknown','duration':3.,
         'opening_or_credits':False,'rms':.05,'clipped_fraction':0.}
    scan={'dataset_id':'all-episodes','series':'test','model':manifest['model'],'sources':manifest['sources'],
          'samples':[new],'summary':{'completed':1,'total':1,'failed':[]}}
    write_json(scan_root/'manifest.json',scan)
    write_json(scan_root/'discovery-groups.json',[{'id':'new','count':8,'cohesion':.8,'episodes':2,'preview_ids':[new['id']]}])
    result=import_discovery(scan_root,root)
    assert len(result['samples'])==2
    store=ReviewStore(root);state=store.state()
    assert state['review']['decisions']==review['decisions']
    assert len(state['progress']['voice_discovery'])==1
    assert state['progress']['coverage'][1]['status']=='not_located'
    assert [p['name'] for p in read_json(root/'registry.json')['characters']]==['甲']
    imported_revision=state['review']['revision']
    import_discovery(scan_root,root)
    assert store.review()['revision']==imported_revision
    store.save({'dataset_id':result['dataset_id'],'revision':imported_revision,
                'changes':{new['id']:{'status':'named','name':'乙'}}})
    assert store.state()['progress']['coverage_complete']
    assert not store.state()['progress']['voice_discovery']
