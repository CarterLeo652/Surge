"""配置校验和签到时间规则，不依赖 AstrBot。"""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Settings:
    refresh_hours: float = 24
    help_negative_ratio: float = 0.01
    loan_payout_ratio: float = 0.1
    inactivity_days: int = 7
    dod_bonus_max: int = 5
    dod_rich_exclusion_ratio: float = 0.1
    refresh_mode: str = "group_interval"
    timezone: str = "UTC"
    daily_time: str = "00:00"
    interval_minutes: int = 1440
    growth_min: int = -5
    growth_max: int = 10
    positive_probability: float = 0.5
    newcomer_days: int = 7
    streak_enabled: bool = True
    streak_grades: str = "2,4,8,16,32"
    pk_default_bet: int = 1
    pk_check_acceptor_length: bool = False
    pk_timeout_seconds: int = 0
    leaderboard_page_size: int = 10

    @classmethod
    def from_mapping(cls, values):
        hours = values.get("refresh_hours", 24)
        if (
            isinstance(hours, bool)
            or not isinstance(hours, (int, float))
            or not math.isfinite(hours)
            or not 1 / 60 <= hours <= 8760
        ):
            raise ValueError("签到刷新间隔应为 1 分钟到 8760 小时之间的数字。")
        result = cls(refresh_hours=hours, interval_minutes=hours * 60)
        result.validate()
        return result

    @property
    def tz(self):
        return ZoneInfo(self.timezone)

    @property
    def grades(self):
        try:
            return tuple(
                sorted(
                    {
                        int(x.strip())
                        for x in self.streak_grades.replace("，", ",").split(",")
                        if x.strip()
                    }
                )
            )
        except (ValueError, AttributeError) as exc:
            raise ValueError("连签档位请填写逗号分隔的整数，例如 2,4,8,16,32。") from exc

    def validate(self):
        if self.refresh_mode not in {"daily", "personal_interval", "group_interval"}:
            raise ValueError("签到刷新模式无效，请在下拉选项中选择。")
        try:
            self.tz
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError("时区无效，例如北京时间填写 Asia/Shanghai。") from exc
        if not isinstance(self.daily_time, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", self.daily_time
        ):
            raise ValueError("刷新时刻请使用 24 小时制 HH:MM，例如 08:00。")
        for key in (
            "growth_min",
            "growth_max",
            "newcomer_days",
            "pk_default_bet",
            "pk_timeout_seconds",
            "leaderboard_page_size",
        ):
            if type(getattr(self, key)) is not int:
                raise ValueError(f"配置 {key} 必须是整数。")
        for key in ("streak_enabled", "pk_check_acceptor_length"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"配置 {key} 必须是开关。")
        if not 1 <= self.interval_minutes <= 525600:
            raise ValueError("签到间隔应为 1～525600 分钟。")
        if not -10000 <= self.growth_min <= self.growth_max <= 10000 or self.growth_max < 1:
            raise ValueError(
                "增长范围应在 -10000～10000 内，最大值至少为 1，最小值不能大于最大值。"
            )
        if (
            isinstance(self.positive_probability, bool)
            or not isinstance(self.positive_probability, (int, float))
            or not math.isfinite(self.positive_probability)
            or not 0 <= self.positive_probability <= 1
        ):
            raise ValueError("正增长概率应为 0～1 之间的数字。")
        if not 0 <= self.newcomer_days <= 365:
            raise ValueError("新手保护天数应为 0～365，0 表示关闭。")
        if any(x < 2 or x > 3660 for x in self.grades):
            raise ValueError("连签奖励档位应为 2～3660 天。")
        if not 1 <= self.pk_default_bet <= 1000000000:
            raise ValueError("默认 PK 赌注应为 1～1000000000 厘米。")
        if not 0 <= self.pk_timeout_seconds <= 604800:
            raise ValueError("挑战有效期应为 0～604800 秒，0 表示不限时。")
        if not 1 <= self.leaderboard_page_size <= 20:
            raise ValueError("排行榜每页应显示 1～20 人。")

    def window(self, now):
        """返回全体共享的本次签到窗口；个人间隔由 next_due 单独计算。"""
        local = datetime.fromtimestamp(now, self.tz)
        hour, minute = map(int, self.daily_time.split(":"))
        if self.refresh_mode == "daily":
            start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if local < start:
                start -= timedelta(days=1)
            return start.timestamp(), (start + timedelta(days=1)).timestamp()
        # 全群周期使用固定锚点，重启和跨日都不重新开始计时。
        anchor = datetime(1970, 1, 1, hour, minute, tzinfo=self.tz).timestamp()
        period = self.interval_minutes * 60
        start = anchor + math.floor((now - anchor) / period) * period
        return start, start + period

    def next_due(self, last, now):
        if last is None:
            return now
        if self.refresh_mode == "personal_interval":
            return last + self.interval_minutes * 60
        start, end = self.window(now)
        return now if last < start else max(end, last)

    def format_time(self, timestamp):
        return datetime.fromtimestamp(timestamp, self.tz).strftime("%m-%d %H:%M")
