from pathlib import Path
import pytest
from PIL import Image
from autoclip.config import Settings
from autoclip.models import Line
from autoclip.identity import load_anchors, file_hash, reference_board, assess_review
from autoclip.speakers import ground_speakers, recognize_scene
from autoclip.util import source_identity, write_json


def evidence():
    lines=[Line(id=10,start=10,end=12,text='这件差事难办'),Line(id=11,start=12.1,end=14,text='八哥的话你们都听见了')]
    frames=[{'id':0,'visible':['R0'],'uncertain':False},{'id':1,'visible':['R1'],'uncertain':False},
            {'id':2,'visible':['R0'],'uncertain':False}]
    anchors=[{'id':'eight','name':'胤禩','aliases':['八哥']},{'id':'crown','name':'胤礽','aliases':['太子']}]
    raw={'lines':[{'evidence_id':10,'speaker':'R0','basis_type':'visual_dialogue_context',
                   'context_ids':[10,11],'frame_ids':[0,2],'reaction_frame_ids':[1],'basis':'连续发言，中间切听话人'}]}
    return raw,lines,[10],frames,[10,11,12],anchors


def test_reaction_shot_does_not_replace_continuing_speaker():
    result=ground_speakers(*evidence())
    assert result[0]['anchor_id']=='eight' and result[0]['reaction_frame_ids']==[1]


@pytest.mark.parametrize('change',[{'speaker':'R1','frame_ids':[1,1],'reaction_frame_ids':[]},
                                    {'frame_ids':[1],'reaction_frame_ids':[1]},
                                    {'context_ids':[999]}, {'speaker':'unknown'}])
def test_single_reaction_frame_duplicate_frame_or_missing_context_cannot_identify_speaker(change):
    args=evidence();args[0]['lines'][0].update(change)
    assert ground_speakers(*args)[0]['anchor_id'] is None


def test_each_reply_must_be_separate_and_covered():
    args=evidence();args[2].append(11)
    with pytest.raises(ValueError,match='逐条'):ground_speakers(*args)


def test_distant_named_context_cannot_identify_a_current_line():
    args=evidence();args[1][1].start=100;args[1][1].end=101
    assert ground_speakers(*args)[0]['anchor_id'] is None


def test_continuous_speech_reuses_cited_faces_across_reaction_shots():
    args=list(evidence())
    args[1]=[Line(id=i,start=i*3,end=i*3+2.9,text='连续发言') for i in range(10,16)]
    args[2]=list(range(10,16))
    args[4]=[30,43,31]
    args[0]['lines']=[{**args[0]['lines'][0],'evidence_id':i,
                      'context_ids':[max(10,i-1),min(15,i+1)]} for i in args[2]]
    turns=ground_speakers(*args)
    assert all(t['anchor_id']=='eight' for t in turns)
    assert turns[-1]['utterance_ids']==list(range(10,16))
    assert turns[-1]['frame_ids']==[0,2]
    # A change of speaker stops the chain; it cannot carry identification
    # through an intervening reply or an unobserved line.
    args[0]['lines'][3].update(speaker='unknown',basis_type='unknown')
    assert ground_speakers(*args)[-1]['anchor_id'] is None


def test_missing_subtitle_cannot_bridge_continuous_speech():
    args=list(evidence())
    args[1]=[Line(id=i,start=i*3,end=i*3+2.9,text='对白') for i in range(10,16)]
    first=args[0]['lines'][0]
    args[0]['lines']=[{**first,'evidence_id':10,'context_ids':[15]},
                       {**first,'evidence_id':15,'context_ids':[10]}]
    args[2]=[10,15];args[4]=[30,46,31]
    assert ground_speakers(*args)[-1]['anchor_id'] is None


def test_wrong_named_basis_is_reclassified_only_with_multiframe_support():
    args=evidence();args[0]['lines'][0]['basis_type']='address_response'
    args[1][1].text='对方的回答没有点名'
    turn=ground_speakers(*args)[0]
    assert turn['anchor_id']=='eight' and turn['basis_type']=='visual_dialogue_context'
    assert turn['claimed_basis_type']=='address_response'
    args[0]['lines'][0]['frame_ids']=[0]
    assert ground_speakers(*args)[0]['anchor_id'] is None


def test_multiple_reviewed_views_keep_one_character_id_and_validate_each_source(tmp_path):
    source=tmp_path/'source.mp4';source.write_bytes(b'source')
    first=tmp_path/'one.jpg';second=tmp_path/'two.jpg'
    for p in (first,second):Image.new('RGB',(200,200),'grey').save(p)
    extra={'source':source_identity(source),'image':second.name,'image_sha256':file_hash(second),
           'status':'reviewed','basis':'另一场明确称呼和同脸对照','reviewer':'fixture'}
    row={'id':'eight','name':'胤禩','aliases':['八哥'],'source':source_identity(source),'image':first.name,
         'image_sha256':file_hash(first),'status':'reviewed','basis':'点名回应','reviewer':'fixture','references':[extra]}
    registry=tmp_path/'registry.json';write_json(registry,{'series':'测试剧','anchors':[row]})
    anchors,images=load_anchors(registry,'测试剧')
    assert len(anchors)==1 and len(anchors[0]['references'])==1
    board=reference_board(anchors,images,tmp_path/'board')
    assert board.exists()
    second.write_bytes(b'tampered')
    with pytest.raises(ValueError,match='补充人物参考图片'):load_anchors(registry,'测试剧')


def test_scene_observations_reuse_across_drafts_and_split_face_and_dialogue_inputs(tmp_path,monkeypatch):
    import autoclip.identity as identity
    import autoclip.ingest as ingest
    image=tmp_path/'reference.jpg';Image.new('RGB',(200,200),'grey').save(image)
    sheet=tmp_path/'sheet.jpg';Image.new('RGB',(960,294),'grey').save(sheet)
    video=tmp_path/'06.mp4';video.write_bytes(b'source')
    anchor={'id':'eight','name':'胤禩','aliases':['八哥']}
    monkeypatch.setattr(identity,'load_anchors',lambda *a:([anchor],[image]))
    monkeypatch.setattr(ingest,'contact_sheet',lambda *a:sheet)
    lines=[Line(id=1,start=10,end=12,text='这差事难办')]
    calls=[]
    class Model:
        def json(self,system,prompt,images):
            calls.append(system)
            if len(calls)==1:
                assert '这差事难办' not in prompt
                return {'frames':[{'id':i,'visible':['R0'],'uncertain':False} for i in (0,1)]}
            assert '这差事难办' in prompt
            return {'lines':[{'evidence_id':1,'speaker':'R0','basis_type':'visual_dialogue_context',
                              'context_ids':[1],'frame_ids':[0,1],'basis':'多帧与连续对话'}]}
    cfg=Settings(continuity_history=tmp_path/'shared')
    first=recognize_scene(video,tmp_path/'draft1',lines,10,12,cfg,Model(),[10.2,11.8])
    second=recognize_scene(video,tmp_path/'draft2',lines,10,12,cfg,Model(),[10.2,11.8])
    assert first['turns'][0]['anchor_id']=='eight'
    assert first['signature']==second['signature'] and len(calls)==2
    assert (tmp_path/'draft2/scene-identity-latest.json').exists()


def test_named_response_cannot_bypass_an_opposing_independent_speaker():
    anchors=[{'id':'eight','name':'胤禩','aliases':['八哥']},{'id':'crown','name':'胤礽','aliases':['太子']}]
    lines=[Line(id=1,start=10,end=11,text='太子你的意思呢'),Line(id=2,start=12,end=13,text='儿臣愿意')]
    units=[{'id':'title','text':'太子提出交出户部'}]
    raw={'units':[{'id':'title','claims':[{'excerpt':'太子提出交出户部','subject':'太子','action':'提出交出',
          'observed_subject':'太子','role':'speaker','verdict':'supported','basis':'explicit_address_response',
          'anchor_ids':[],'evidence_ids':[1,2],'frame_ids':[0],'reason':'字幕点名'}]}]}
    scene={'turns':[{'anchor_id':'eight','evidence_ids':[1,2]}]}
    assert not assess_review(raw,units,lines,anchors,[10.5],'雍正王朝',scene)['passed']
