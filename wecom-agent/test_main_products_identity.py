"""Delayed plan pages must become verifiable without accepting a wrong identity."""
from pathlib import Path
from types import SimpleNamespace
import sys, unittest
from unittest.mock import AsyncMock, Mock
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'投流'))
from 巨量千川.main_products import MAIN_AAVID, MAIN_PRODUCTS, verify_account
A,B=MAIN_PRODUCTS[:2]
URL=f'https://qianchuan.jinritemai.com/uni-prom/setting?aavid={MAIN_AAVID}&adId={A.plan_id}'
LOADING=f'鲜会鲜 ID：{MAIN_AAVID}\n加载中...'
READY=f'鲜会鲜 ID：{MAIN_AAVID}\n计划 ID：{A.plan_id}'
def page(bodies,url=URL):
    inner=AsyncMock(side_effect=bodies)
    return SimpleNamespace(url=url,locator=Mock(return_value=SimpleNamespace(inner_text=inner)),wait_for_function=AsyncMock())
class IdentityLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_loading_shell_waits_then_requires_exact_plan(self):
        p=page([LOADING,READY])
        self.assertEqual(await verify_account(p,plan_id=A.plan_id),READY)
        p.wait_for_function.assert_awaited_once()
        self.assertEqual(p.wait_for_function.await_args.kwargs['arg'],{'account':MAIN_AAVID,'plan':A.plan_id})
        self.assertEqual(p.wait_for_function.await_args.kwargs['timeout'],30000)
    async def test_ready_plan_does_not_wait(self):
        p=page([READY]);self.assertEqual(await verify_account(p,plan_id=A.plan_id),READY)
        p.wait_for_function.assert_not_awaited()
    async def test_wrong_url_or_visible_identity_is_never_retried(self):
        for body,url in [(LOADING,URL.replace(MAIN_AAVID,'wrong')),(LOADING,URL.replace(A.plan_id,B.plan_id)),('ID：wrong\n加载中',URL),(f'ID：{MAIN_AAVID} ID：{B.plan_id} 加载中',URL)]:
            with self.subTest(body=body,url=url):
                p=page([body],url)
                with self.assertRaises(RuntimeError):await verify_account(p,plan_id=A.plan_id)
                p.wait_for_function.assert_not_awaited()
    async def test_missing_plan_without_loading_evidence_fails_closed(self):
        p=page([f'ID：{MAIN_AAVID}'])
        with self.assertRaises(RuntimeError):await verify_account(p,plan_id=A.plan_id)
        p.wait_for_function.assert_not_awaited()
    async def test_wrong_plan_after_loading_is_rejected(self):
        p=page([LOADING,f'ID：{MAIN_AAVID} ID：{B.plan_id}'])
        with self.assertRaises(RuntimeError):await verify_account(p,plan_id=A.plan_id)
    async def test_account_or_url_changed_after_wait_is_rejected(self):
        for body,redirect in [('ID：wrong ID：'+A.plan_id,None),(READY,URL.replace(A.plan_id,B.plan_id))]:
            p=page([LOADING,body])
            if redirect:
                async def navigate(*args,**kwargs):p.url=redirect
                p.wait_for_function.side_effect=navigate
            with self.assertRaises(RuntimeError):await verify_account(p,plan_id=A.plan_id)
    async def test_loading_timeout_never_allows_submission(self):
        p=page([LOADING]);p.wait_for_function.side_effect=TimeoutError('fixture timeout')
        with self.assertRaisesRegex(RuntimeError,'禁止提交'):await verify_account(p,plan_id=A.plan_id)
    async def test_account_only_auth_page_retains_existing_check(self):
        p=page([f'ID：{MAIN_AAVID}'])
        await verify_account(p)
        p.wait_for_function.assert_not_awaited()
if __name__=='__main__':unittest.main()
