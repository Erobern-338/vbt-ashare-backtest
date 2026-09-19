"""项目级常量：路径、复权模式、回测默认参数。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache" / "daily"

# 后复权数值稳定、不随分红追溯改写，回测默认为它。
# 前复权 qfq 的历史价格会在每次除权后被整段重写，缓存不可复现。
DEFAULT_ADJUST = "hfq"

DEFAULT_INIT_CASH = 100_000.0
# 对称费率，近似覆盖双边成本：佣金 0.0003 + 印花税 0.0005(卖出) + 过户费
DEFAULT_FEES = 0.0008
DEFAULT_FREQ = "1D"

# 涨跌停判定阈值(%)。主板 10%，创业板/科创板 20%，留 0.2 的余量。
LIMIT_PCT_MAIN = 9.8
LIMIT_PCT_GEM = 19.8
