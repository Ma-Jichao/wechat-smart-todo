"""Isolated DB regressions and real process-exit injection; no network sends."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from support import load_test_config
from app import tools
from app.config import load, WeixinConfig
from app.db import Database
from app.scheduler import Scheduler
from app.sender import DeliverySender

ROOT = Path(__file__).resolve().parent.parent

async def scenario():
    base=load_test_config()
    account='test-reliability-'+str(uuid.uuid4())
    user='test-reliability-user-'+str(uuid.uuid4())
    wx=replace(base.weixin, account_id=account, allowed_user_id=user, base_url='https://example.invalid', token='test-token')
    delivery_cfg=replace(base.delivery,min_interval_secs=0,lease_secs=1)
    db=Database(base.db,min_size=1,max_size=2)
    await db.open()
    sender=DeliverySender(db,wx,delivery_cfg,base.limits)
    await db.execute("INSERT INTO bot.bot_state(account_id,user_id,context_token) VALUES (%s,%s,'test-context')",(account,user))
    async def task(content):
        return await tools.create_task(db,user,content,fire_at=tools.now_shanghai()+timedelta(hours=1),phrase=None,source_inbox_id=None,max_chars=2000)
    async def due(t):
        await db.execute("UPDATE bot.deliveries SET next_attempt_at=now() WHERE task_id=%s AND status='pending'",(t.id,))
    class Stub:
        calls=0
        async def send_text(self,*args):
            self.calls+=1
            return {'ret':0}
    stub=Stub();sender._sender=stub
    try:
        t=await task('完成阻塞提醒')
        await db.execute("UPDATE bot.deliveries SET status='blocked_context' WHERE task_id=%s",(t.id,))
        await tools.complete_task(db,user,t.id)
        await sender.requeue_blocked_context()
        row=await db.fetch_one('SELECT status FROM bot.deliveries WHERE task_id=%s',(t.id,))
        assert row['status']=='canceled',row
        print('[通过] 完成事项后，blocked_context 不再被入站消息解封')

        t=await task('修改阻塞提醒')
        await db.execute("UPDATE bot.deliveries SET status='blocked_auth' WHERE task_id=%s",(t.id,))
        changed=await tools.update_task(db,user,t.id,content='修改后的内容')
        row=await db.fetch_one('SELECT status FROM bot.deliveries WHERE task_id=%s AND task_version=%s',(t.id,t.version))
        assert row['status']=='superseded'
        await tools.cancel_task(db,user,t.id)
        print('[通过] 修改事项作废 blocked_auth 旧版本，取消停止新版本')

        t=await task('限速期间被完成')
        await due(t)
        claimed=await sender.claim_one();assert claimed['task_id']==t.id
        sender._next_send_at=time.monotonic()+0.1
        async def finish_during_wait():
            await asyncio.sleep(0.02)
            await tools.complete_task(db,user,t.id)
        await asyncio.gather(sender.deliver_one(claimed),finish_during_wait())
        assert stub.calls==0,'stale reminder was sent'
        row=await db.fetch_one('SELECT status FROM bot.deliveries WHERE id=%s',(claimed['id'],))
        assert row['status']=='superseded'
        print('[通过] 限速等待期间完成事项，发送前校验阻止旧提醒')

        t=await task('超时停止重试')
        await due(t)
        await db.execute('UPDATE bot.deliveries SET attempts=%s WHERE task_id=%s',(base.delivery.max_attempts-1,t.id))
        class TimeoutStub:
            async def send_text(self,*args):raise asyncio.TimeoutError()
        sender._sender=TimeoutStub()
        claimed=await sender.claim_one();assert claimed['task_id']==t.id
        await sender.deliver_one(claimed)
        row=await db.fetch_one('SELECT status,attempts FROM bot.deliveries WHERE id=%s',(claimed['id'],))
        assert row['status']=='permanent_failed' and row['attempts']==base.delivery.max_attempts,row
        assert (await tools.get_task(db,user,t.id)).status=='open'
        print('[通过] 超时达到次数上限停止重试，事项仍然待办')

        rec=await tools.create_task(db,user,'重复旧文案',fire_at=None,phrase=None,source_inbox_id=None,max_chars=2000,repeat_times=['08:15'],skip_holidays=False)
        import app.scheduler as scheduler_module
        original=scheduler_module.now_shanghai
        fixed=tools.now_shanghai().replace(hour=8,minute=0,second=0,microsecond=0)
        scheduler_module.now_shanghai=lambda:fixed
        try:
            scheduler=Scheduler(db,wx,base.digest,base.limits)
            assert await scheduler.ensure_recurring_reminders()==1
            await tools.update_task(db,user,rec.id,content='重复新文案')
            assert await scheduler.ensure_recurring_reminders()==1
            assert await scheduler.ensure_recurring_reminders()==0
            row=await db.fetch_one('SELECT status,content_snapshot FROM bot.deliveries WHERE task_id=%s',(rec.id,))
            assert row['status']=='pending' and '重复新文案' in row['content_snapshot'],row
            await tools.cancel_task(db,user,rec.id)
        finally:scheduler_module.now_shanghai=original
        print('[通过] 当天未发送的重复提醒更新文案后恢复，仍保持幂等')

        t=await task('接口接受后进程退出')
        await due(t)
        child_code = r'''
import asyncio, os, sys
from dataclasses import replace
from pathlib import Path
from app.config import load
from app.db import Database
from app.sender import DeliverySender
async def main():
    cfg=load();db=Database(cfg.db,min_size=1,max_size=1);await db.open()
    wx=replace(cfg.weixin,account_id=sys.argv[1],allowed_user_id=sys.argv[2],base_url='https://example.invalid',token='test-token')
    sender=DeliverySender(db,wx,replace(cfg.delivery,lease_secs=1,min_interval_secs=0),cfg.limits)
    row=await sender.claim_one()
    assert row is not None
    class AcceptedThenExit:
        async def send_text(self,*args):
            with open(sys.argv[3],'a') as f:
                f.write('mock-interface-accepted\n');f.flush();os.fsync(f.fileno())
            os._exit(23)
    sender._sender=AcceptedThenExit()
    await sender.deliver_one(row)
asyncio.run(main())
'''
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'accepted.txt'
            env=dict(os.environ, PYTHONPATH=str(ROOT), REMINDER_CONFIG=os.environ["REMINDER_TEST_CONFIG"])
            result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',child_code,account,user,str(marker)],cwd=ROOT,env=env,capture_output=True,text=True)
            assert result.returncode==23,result.stderr
            assert marker.read_text().splitlines()==['mock-interface-accepted']
            await asyncio.sleep(1.2)
            assert await sender.reclaim_expired()>=1
            class AcceptAgain:
                async def send_text(self,*args):
                    with marker.open('a') as f:f.write('mock-interface-accepted\n')
                    return {'ret':0}
            sender._sender=AcceptAgain()
            claimed=await sender.claim_one();assert claimed['task_id']==t.id
            await sender.deliver_one(claimed)
            assert len(marker.read_text().splitlines())==2
            row=await db.fetch_one('SELECT status FROM bot.deliveries WHERE id=%s',(claimed['id'],))
            assert row['status']=='accepted'
        print('[通过] 真实子进程在模拟接口接受后退出：租约恢复重发；已确认此边界会重复')
    finally:
        for table in ('deliveries','task_changes'):
            await db.execute(f'DELETE FROM bot.{table} WHERE task_id IN (SELECT id FROM bot.tasks WHERE user_id=%s)',(user,))
        await db.execute('DELETE FROM bot.tasks WHERE user_id=%s',(user,))
        await db.execute('DELETE FROM bot.bot_state WHERE account_id=%s',(account,))
        await db.close()

if __name__=='__main__':asyncio.run(scenario())
