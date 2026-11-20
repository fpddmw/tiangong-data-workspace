"""One read-only current view; raw original history remains at work-show --detail."""

def current(root,*,task_id=None,request_id=None,limit=10,offset=0,detail=False):
    from harness_snapshot import read
    return read(root,task_id=task_id,request_id=request_id,limit=limit,offset=offset,detail=detail)
