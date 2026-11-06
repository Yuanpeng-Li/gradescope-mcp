import copy
import json
from types import SimpleNamespace

from gradescope_mcp import operations
from gradescope_mcp.tools import grading_plans as plans


def world(monkeypatch):
    states={s:{'question':{'id':2,'weight':10,'scoring_type':'positive','floor':True,'ceiling':True},
              'submission':{'id':int(s),'graded':False}, 'evaluation':{'points':0,'comments':''},
              'rubric_items':[], 'urls':{'save_grade':f'/courses/1/questions/2/submissions/{s}/save_grade'}} for s in ('3','4')}
    sent=[]
    monkeypatch.setattr(plans.grading,'_get_grading_context',lambda c,q,s:{'props':copy.deepcopy(states[s])})
    def post(ctx,payload):
        sid=str(ctx['props']['submission']['id']);sent.append(sid)
        states[sid]['evaluation']=dict(payload['question_submission_evaluation'])
        states[sid]['submission']['graded']=True
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(plans.grading,'_post_save_grade',post)
    monkeypatch.setattr(plans.grading,'_read_back_grade',lambda *a:{'mismatches':[],'score':5,'graded':True})
    prepared=json.loads(plans.prepare_grade_plan('1','2',[{'submission_id':s,'point_adjustment':5} for s in states]))
    return prepared,states,sent


def test_plan_replay_does_not_resend(monkeypatch):
    p,states,sent=world(monkeypatch)
    assert not sent
    result=json.loads(plans.execute_grade_plan(p['operation_token'],p['request_digest'],True))
    assert result['status']=='completed' and sent==['3','4']
    plans.execute_grade_plan(p['operation_token'],p['request_digest'],True)
    assert sent==['3','4']


def test_snapshot_change_refuses_write(monkeypatch):
    p,states,sent=world(monkeypatch);states['3']['question']['ceiling']=False
    r=json.loads(plans.execute_grade_plan(p['operation_token'],p['request_digest'],True))
    assert r['rows'][0]['state']=='stale' and not sent


def test_unknown_save_is_not_repeated(monkeypatch):
    p,states,sent=world(monkeypatch)
    def timeout(*a):
        sent.append('attempt');raise TimeoutError('lost response')
    monkeypatch.setattr(plans.grading,'_post_save_grade',timeout)
    for _ in range(2):
        r=json.loads(plans.execute_grade_plan(p['operation_token'],p['request_digest'],True))
        assert r['status']=='needs_review'
    assert sent==['attempt']


def test_cancel_and_calibration(monkeypatch):
    p,_,sent=world(monkeypatch)
    assert json.loads(plans.calibrate_grade_plan(p['operation_token'],{'3':5}))['mean_absolute_error']==0
    operations.cancel_operation(p['operation_token'])
    r=json.loads(plans.execute_grade_plan(p['operation_token'],p['request_digest'],True))
    assert r['status']=='cancelled' and not sent


def test_plan_digest_tamper(monkeypatch):
    p,_,sent=world(monkeypatch);op=operations.load(p['operation_token']);op['request']['grades'][0]['point_adjustment']=9;operations.save(op)
    assert plans.execute_grade_plan(p['operation_token'],p['request_digest'],True).startswith('Error:')
    assert not sent
