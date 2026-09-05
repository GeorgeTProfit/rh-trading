"""Desk configuration — single source of truth for all agents."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
import json, os, pathlib

HERE = pathlib.Path(__file__).parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True, parents=True)


@dataclass
class DeskConfig:
    # Wallet sources
    gmgn_api_base: str = "https://goapi.gmgn.ai"
    axiom_api_base: str = "https://axiom.xyz/api"
    trench_url: str = "https://robinhoodtrenches.com"

    # Wallet pipeline
    wallet_db_path: str = str(DATA / "wallet_db.json")
    wallet_poll_seconds: int = 14_400  # 4 hours
    max_wallets: int = 250
    min_winrate_14d: float = 0.55
    drop_winrate_7d: float = 0.40
    wallet_refresh_hours: list = field(default_factory=lambda: [0, 4, 8, 12, 16, 20])

    # Signal pipeline
    signal_queue_path: str = str(DATA / "signal_queue.json")
    trade_log_path: str = str(DATA / "trade_log.jsonl")
    positions_path: str = str(DATA / "positions.json")
    desk_state_path: str = str(DATA / "desk_state.json")
    pulse_cache_path: str = str(DATA / "pulse_cache.json")
    pulse_cache_ttl: int = 600  # 10 minutes

    # SCOUT
    scout_interval_seconds: int = 60
    scout_block_window: int = 3
    max_signal_age_seconds: int = 300

    # Scoring thresholds (Zynex's evolved tuning)
    historian_threshold: float = 0.20    # below = DROP (saves $0.08/call)
    composite_threshold: float = 0.31    # Zynex's sweet spot after days 5-9
    elite_threshold: float = 0.40        # over-correction zone

    # PULSE
    pulse_go_minimum: float = 0.15       # below = desk standdown
    btc_drop_alert_pct: float = -4.0
    perp_funding_sharp: float = -0.001

    # Execution
    max_position_eth: float = 0.002
    max_risk_fraction: float = 0.0005
    kelly_multiplier: float = 0.25
    daily_stop_fraction: float = 0.02
    max_daily_loss_eth: float = 0.001
    max_drawdown_fraction: float = 0.10
    max_trades_per_day: int = 3
    cooldown_seconds: int = 600

    # Exit barriers (from position_executor.py defaults)
    stop_loss_pct: float = 0.12      # -12%
    take_profit_pct: float = 0.20    # +20%
    trail_activation: float = 0.10   # trail starts after 10% gain
    trail_delta: float = 0.05        # trail 5% below peak
    time_limit_seconds: int = 600    # 10 min max hold

    # LLM
    llm_model: str = "deepseek-v4-flash-0731"   # fast + cheap
    llm_provider: str = "engy"
    devil_model: str = "deepseek-v4-flash-0731"  # same model; context is the edge
    max_tokens: int = 512
    temperature: float = 0.3

    # Chain
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    weth: str = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    swap_router: str = "0xcaf681a66d020601342297493863e78c959e5cb2"
    pool_fee: int = 10_000
    slippage_bps: int = 500
    max_gas_cost_eth: float = 0.0015
    approved_tokens: list = field(default_factory=lambda: ["0x90a71817bda6dac8c3a28bbfd877b02d667ae2f9"])
    denied_tokens: list = field(default_factory=lambda: ["0x7f04da8cc451dddfbf80d6fa3aae3ee0642f8ab9"])

    @classmethod
    def from_env(cls) -> "DeskConfig":
        cfg = cls()
        for key in dir(cfg):
            if key.startswith("_"):
                continue
            env_val = os.environ.get(f"DESK_{key.upper()}")
            if env_val is not None:
                current = getattr(cfg, key)
                if isinstance(current, bool):
                    setattr(cfg, key, env_val.lower() in ("1", "true", "yes"))
                elif isinstance(current, int):
                    setattr(cfg, key, int(env_val))
                elif isinstance(current, float):
                    setattr(cfg, key, float(env_val))
                elif isinstance(current, str):
                    setattr(cfg, key, env_val)
        return cfg

    def save_state(self, **overrides):
        state = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        state.update(overrides)
        state.pop("wallet_db_path", None)
        state.pop("signal_queue_path", None)
        state.pop("trade_log_path", None)
        state.pop("positions_path", None)
        state.pop("desk_state_path", None)
        state.pop("pulse_cache_path", None)
        with open(str(DATA / "desk_state.json"), "w") as f:
            json.dump(state, f, indent=2, default=str)
        return state


CONFIG = DeskConfig.from_env()
