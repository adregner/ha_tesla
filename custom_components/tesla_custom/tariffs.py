"""foo"""

from collections.abc import Iterator
from datetime import date, datetime, timedelta
import logging

from homeassistant.util import dt

type Season = str | None
type Period = str | None

# logger = logging.getLogger(__name__)


class TariffParser:
    """Extracts info from Tesla API tariff response"""

    def __init__(self, data: dict):
        if "response" in data:
            data = data["response"]
        # logger.warning("init TariffParser with data: %s", data)
        self.data = data

    def season(self, when: datetime) -> Season:
        """Returns the season for a given date."""
        for season_name, season in self.data["seasons"].items():
            # logger.warning("checking season %r: %r", season_name, season)
            start = day_in(when, season["fromMonth"], season["fromDay"])
            end = day_in(when, season["toMonth"], season["toDay"])
            if start <= when.date() <= end:
                return season_name
        # logger.warning("No season found for date: %s", when)
        return None

    def season_period(self, when: datetime) -> tuple[Season, Period]:
        """Returns the season and time-of-use period name for a given date."""
        season_name = self.season(when)
        if season_name is None:
            # logger.warning("No season for rate found for date: %s", when)
            return (None, None)
        period_name, _ = self.get_period(season_name, when)
        return season_name, period_name

    def get_periods(self, season_name: Season) -> Iterator[tuple[Period, dict]]:
        """Returns an iterator over the time-of-use periods for a given season."""
        season = self.data["seasons"].get(season_name)
        if not season:
            return
        for period_name, periods in season["tou_periods"].items():
            for period in periods:
                yield (period_name, period)

    def get_period(self, season_name: Season, when: datetime) -> tuple[Period, dict]:
        """Returns the time-of-use period name and details for a date in the given season."""
        for period_name, period in self.get_periods(season_name):
            if period["fromDayOfWeek"] <= when.weekday() <= period["toDayOfWeek"]:
                start = time_on(when, period["fromHour"], period["fromMinute"])
                end = time_on(when, period["toHour"], period["toMinute"])
                end = ensure_greater(end, start)
                if start <= when <= end:
                    return (period_name, period)
                # else:
                #     logger.warning("not the time %r <= %r <= %r", start, when, end)
            # else:
            #     logger.warning(
            #         "not the day of week %r <= %r <= %r",
            #         period["fromDayOfWeek"],
            #         when.weekday(),
            #         period["toDayOfWeek"],
            #     )
        # logger.warning("No period found for season date: %r %s", season_name, when)
        return (None, {})

    def get_period_end(
        self, when: datetime, season_name: Season | None = None
    ) -> datetime:
        """Returns the end time of the time-of-use period for a given date."""
        if not season_name:
            season_name = self.season(when)
        period_name, period = self.get_period(season_name, when)
        # logger.warning(
        #     "get_period_end() season:%r period_name:%r period:%r",
        #     season_name,
        #     period_name,
        #     period,
        # )
        period_end = time_on(when, period["toHour"], period["toMinute"])
        return ensure_greater(period_end, when)

    def get_rate(self, season_name: Season, period_name: Period) -> float:
        """Returns the energy charge for a given season and period."""
        return self.data["energy_charges"][season_name][period_name]

    def find(self, when: datetime) -> tuple[Season, Period, float]:
        """Returns the season, period, and rate for a given date."""
        season, period = self.season_period(when)
        rate = self.get_rate(season, period)
        return (season, period, rate)

    def for_selling(self) -> "TariffParser":
        """Returns a new instance for the sell rate tariff."""
        # assume this is already the "selling" instance
        if "sell_tariff" not in self.data:
            return self
        return TariffParser(self.data["sell_tariff"])


def day_in(when: datetime, month: int, day: int) -> date:
    """Returns a date object for the given month and day in the year
    of the given datetime."""
    try:
        return date(when.year, month, day)
    except ValueError:
        # stale data from the API (e.g. from last year) can result in trying to
        # create `date(2025, 2, 29)` which fails because the leap-year was last year
        if month == 2 and day == 29:
            return date(when.year, month, day - 1)
        raise


def time_on(when: datetime, hour: int, minute: int) -> datetime:
    """Returns a datetime object for the given hour and minute on the same day
    as the given datetime.  Localizes to the default time zone in homeassistant."""
    return datetime(
        when.year, when.month, when.day, hour, minute, tzinfo=dt.DEFAULT_TIME_ZONE
    )


def ensure_greater(value: datetime, reference: datetime) -> datetime:
    """Ensures the given datetime is greater than the reference datetime.
    If it is not, it will be adjusted to the next day."""
    if value < reference:
        # logger.warning("Ensuring greater: %r <= %r", value, reference)
        return value + timedelta(days=1)
    return value
