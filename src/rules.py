from __future__ import annotations
from .domain import ConflictError, ValidationError, parse_timestamp
TITLE='企业排污许可与超标处置'; ENTITY='排污事件'; ID_PREFIX='ED'
RECTIFICATION_ENTITY='整改台账'; RECTIFICATION_STATES=['open', 'closed']; REGISTER_STATES=['reported', 'assessing', 'remediation']
SEVERITIES=['normal', 'watch', 'exceedance', 'major']; STATES=['reported', 'assessing', 'remediation', 'inspection', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['remediation'], 'remediation': ['inspection'], 'inspection': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['compliance_officer'], 'remediation': ['operator'], 'inspection': ['compliance_officer'], 'closed': ['director']}
CREATE_ROLES=set(['operator', 'compliance_officer']); RECORD_ROLES=set(['operator', 'compliance_officer']); AUDIT_ROLES=set(['director', 'viewer']); VIEW_ROLES=set(['operator', 'compliance_officer', 'director', 'viewer'])
REGISTER_ROLES=set(['compliance_officer']); RETEST_ROLES=set(['operator', 'compliance_officer']); PARAMETER_ROLES=set(['operator', 'compliance_officer']); DEADLINE_ROLES=set(['compliance_officer', 'director'])
SEVERITY_WEIGHT={'normal': 1.0, 'watch': 3.0, 'exceedance': 6.0, 'major': 9.0}; DEADLINE_HOURS={'normal': 72, 'watch': 24, 'exceedance': 8, 'major': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records,open_rectifications=0):
    if target not in TERMINAL_STATES: return []
    blockers=[]
    if open_records>0: blockers.append("仍有未关闭事项")
    if open_rectifications>0: blockers.append("仍有未结束整改")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def judge_retest(reading,threshold,submitted_at,due_at):
    if reading>threshold: return 'fail'
    if submitted_at>due_at: return 'fail'
    return 'pass'
def closure_blockers(rectification,current_retests):
    blockers=[]
    if rectification["status"]!=RECTIFICATION_STATES[0]: blockers.append("整改已关闭")
    latest=current_retests[-1] if current_retests else None
    if latest is None: blockers.append("尚无复测结论")
    elif latest["conclusion"]!='pass': blockers.append("最新复测结论不合格")
    elif latest["submitted_by"]==rectification["assignee"]: blockers.append("复测须由责任人以外的人员完成")
    return blockers
def inspection_blockers(rectification,current_retests,threshold,now):
    if rectification is None or rectification["status"]==RECTIFICATION_STATES[-1]: return []
    blockers=[]
    if now>parse_timestamp(rectification["due_at"],"due_at") and not current_retests: blockers.append("逾期未交复测")
    if current_retests and current_retests[-1]["reading"]>threshold: blockers.append("新监测读数仍高于许可限值")
    blockers.append("整改未关闭")
    return blockers
