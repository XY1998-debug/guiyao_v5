"""归爻 V5 — 综合验证测试套件
用法: cd H:\归爻 && .venv\Scripts\python.exe -m tests -v
"""
import unittest, sys, os, tempfile, numpy as np
sys.path.insert(0, r"H:\归爻")

# ── 1. 费率 ──
class TestCommissionRates(unittest.TestCase):
    def test_guotai_stock_rate(self):
        c = open(r"H:\归爻\strategies\stock_breakout.py", encoding="utf-8").read()
        self.assertIn("1.00008", c, "买入乘数")
        self.assertIn("0.99892", c, "卖出乘数")

    def test_etf_rate(self):
        c = open(r"H:\归爻\strategies\etf_rotation.py", encoding="utf-8").read()
        self.assertIn("1.00005", c, "ETF买入")
        self.assertIn("0.99995", c, "ETF卖出")

    def test_position_sizer_rates(self):
        from engine.position_sizer import PositionSizer
        s = PositionSizer(total_capital=100000)
        self.assertAlmostEqual(s.fee_buy, 0.00008)
        self.assertAlmostEqual(s.fee_sell, 0.00108)

    def test_param_search_uses_guotai(self):
        c = open(r"H:\归爻\engine\param_search.py", encoding="utf-8").read()
        self.assertIn("0.00008", c)

    def test_backtest_backup_rates(self):
        c = open(r"H:\归爻\engine\backtest.py", encoding="utf-8").read()
        self.assertIn("BUY_FEE = 0.00008", c)
        self.assertIn("SELL_FEE = 0.00108", c)

# ── 2. 阴影账本 ──
class TestShadowLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        from engine.shadow_ledger import ShadowLedger
        self.l = ShadowLedger(self.tmp)

    def tearDown(self):
        os.unlink(self.tmp)

    def test_buy_sell_pnl(self):
        sid = self.l.log_ai_signal("000001", "buy", 10.0, 1000)
        self.l.log_shadow_execution(sid, "000001", "buy", 10.0, 1000, 5.0)
        sid2 = self.l.log_ai_signal("000001", "sell", 11.0, 500)
        self.l.log_shadow_execution(sid2, "000001", "sell", 11.0, 500, 3.0)
        self.assertIn("已平仓盈亏", self.l.weekly_report())

    def test_position_independence(self):
        self.l.log_shadow_execution(
            self.l.log_ai_signal("000001", "buy", 10.0, 500), "000001", "buy", 10.0, 500, 2.5)
        self.l.log_shadow_execution(
            self.l.log_ai_signal("000002", "buy", 20.0, 300), "000002", "buy", 20.0, 300, 2.5)
        self.l.log_shadow_execution(
            self.l.log_ai_signal("000001", "sell", 11.0, 500), "000001", "sell", 11.0, 500, 2.5)
        self.assertEqual(self.l._get_held("000002", "SHADOW"), 300)

# ── 3. 仓位管理 ──
class TestPositionSizer(unittest.TestCase):
    def test_normal_buy(self):
        from engine.position_sizer import PositionSizer
        s = PositionSizer(total_capital=100000)
        self.assertGreater(s.calc_shares(10.0, 1.0, 50000, 50000, "chop"), 0)

    def test_no_cash_no_buy(self):
        from engine.position_sizer import PositionSizer
        s = PositionSizer(total_capital=100000)
        self.assertEqual(s.calc_shares(10.0, 1.0, 100, 0, "chop"), 0)

# ── 4. 状态检测 ──
class TestRegime(unittest.TestCase):
    def test_classify(self):
        from engine.state_evaluator import classify
        self.assertEqual(classify(80, 0.5, 50), "bull")
        self.assertEqual(classify(20, 0.5, 50), "bear")
        self.assertEqual(classify(50, 0.5, 50), "chop")
        self.assertEqual(classify(50, 0.05, 400), "extreme")

# ── 5. 统计指标 ──
class TestMetrics(unittest.TestCase):
    def test_dsr(self):
        from engine.numpy_metrics import calc_dsr
        self.assertAlmostEqual(calc_dsr(0.0, 60, 50), 0.0)
        self.assertIsInstance(calc_dsr(0.5, 60, 50), float)

    def test_calmar(self):
        from engine.numpy_metrics import calc_calmar
        self.assertIsInstance(calc_calmar(np.random.randn(250) * 0.01), float)

    def test_ks(self):
        from engine.numpy_metrics import calc_ks
        d, p = calc_ks(np.random.randn(100), np.random.randn(100))
        self.assertGreater(d, 0)
        self.assertGreater(p, 0)

# ── 6a. Numba 内核冒烟测试 ──
class TestKernelSmoke(unittest.TestCase):
    def test_single_stock_buy(self):
        from engine.backtest_kernel_v3 import backtest_kernel_v3
        import numpy as np
        p = np.array([10.0]); s = np.array([[1]]); a = np.array([1.0])
        l = np.array([9.0])
        c = np.full((1,1),100000.0); ps = np.zeros((1,1)); cs = np.zeros((1,1))
        bi = np.zeros((1,1),dtype=np.int8); bo = np.zeros((1,1),dtype=np.int8)
        yl = np.zeros(1,dtype=np.int8)
        backtest_kernel_v3(p,s,a,l,100000.0,c,ps,cs,bi,bo,yl,1,1,2.5,0.00008,0.001,5.0)
        self.assertGreater(ps[0,0], 0, "买入后持仓 > 0")
        self.assertLess(c[0,0], 100000, "现金减少")

    def test_limit_down_blocks_sell(self):
        from engine.backtest_kernel_v3 import backtest_kernel_v3
        import numpy as np
        p = np.array([10.0, 10.0]); a = np.array([1.0, 1.0])
        l = np.array([10.0-1e-5, 9.0]); s = np.array([[-1, 1]])
        c = np.full((1,1),100000.0); ps = np.array([[100.0,0.0]]); cs = np.zeros((1,2))
        bi = np.zeros((1,2),dtype=np.int8); bo = np.zeros((1,2),dtype=np.int8)
        yl = np.zeros(2,dtype=np.int8)
        backtest_kernel_v3(p,s,a,l,100000.0,c,ps,cs,bi,bo,yl,1,2,2.5,0.00008,0.001,5.0)
        self.assertEqual(ps[0,0], 100.0, "跌停未卖出")

# ── 6. 编译验证 ──
class TestCompilation(unittest.TestCase):
    def test_all(self):
        for root in ["engine", "strategies", "scripts"]:
            for fn in os.listdir(f"H:/归爻/{root}"):
                if fn.endswith(".py") and fn != "__init__.py":
                    compile(open(f"H:/归爻/{root}/{fn}", encoding="utf-8").read(), fn, "exec")

if __name__ == "__main__":
    unittest.main(verbosity=2)
