"""foo"""

from collections.abc import Iterator
from datetime import date, datetime, timedelta
import logging
import sys
import json
from typing import NotRequired, TypedDict

try:
    from homeassistant.util import dt

    DEFAULT_TIMEZONE = dt.DEFAULT_TIME_ZONE
except (ImportError, ModuleNotFoundError):
    if __name__ != "__main__":
        raise
    import dateutil

    DEFAULT_TIMEZONE = dateutil.tz.tzoffset("PDT", -25200)

type SeasonName = str
type PeriodName = str


class Period(dict):
    """Represents a time-of-use period within a season."""

    fromDayOfWeek: int
    toDayOfWeek: int
    fromHour: int
    fromMinute: int
    toHour: int
    toMinute: int

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def start_time_on(self, when: datetime) -> datetime:
        return ensure_greater(
            time_on(when, self["fromHour"], self["fromMinute"]),
            when,
        )

    def end_time_on(self, when: datetime) -> datetime:
        return ensure_greater(
            time_on(when, self["toHour"], self["toMinute"]),
            when,
        )


class Season(TypedDict):
    """Represents a season in the tariff structure."""

    fromMonth: int
    fromDay: int
    toMonth: int
    toDay: int
    tou_periods: dict[PeriodName, list[Period]]


class TariffData(TypedDict):
    """Represents the tariff data structure."""

    seasons: dict[SeasonName, Season]
    energy_charges: dict[SeasonName, dict[PeriodName, float]]
    sell_tariff: NotRequired["TariffData"]


logger = logging.getLogger(__name__)


class TariffParser:
    """Extracts info from Tesla API tariff response"""

    data: TariffData

    def __init__(self, data: TariffData | dict):
        if "response" in data:
            self.data = data["response"]  # type: ignore[typeddict-item,assignment]
        else:
            self.data = data  # type: ignore[assignment]

        with open('/tmp/tesla_component_tariff_data.json', 'w') as fd:
            json.dump(self.data, fd, indent=4)

    def season(self, when: datetime) -> SeasonName:
        """Returns the season for a given date."""
        for season_name, season in self.data["seasons"].items():
            # logger.warning("checking season %r: %r", season_name, season)
            start = day_in(when, season["fromMonth"], season["fromDay"])
            end = day_in(when, season["toMonth"], season["toDay"])
            end = ensure_greater_year(end, start)
            if start <= when.date() < end:
                return season_name
        logger.warning("No season found for date: %s", when)
        return None

    def season_period(self, when: datetime) -> tuple[SeasonName, PeriodName]:
        """Returns the season and time-of-use period name for a given date."""
        season_name = self.season(when)
        if season_name is None:
            # logger.warning("No season for rate found for date: %s", when)
            return (None, None)
        period_name, _ = self.get_period(season_name, when)
        return season_name, period_name

    def get_periods(
        self, season_name: SeasonName
    ) -> Iterator[tuple[PeriodName, Period]]:
        """Returns an iterator over the time-of-use periods for a given season."""
        season = self.data["seasons"].get(season_name)
        if not season:
            return
        for period_name, periods in season["tou_periods"].items():
            for period in periods:
                yield (period_name, Period(**period))

    def get_period(
        self, season_name: SeasonName, when: datetime
    ) -> tuple[PeriodName, Period]:
        """Returns the time-of-use period name and details for a date in the given season."""
        for period_name, period in self.get_periods(season_name):
            if period["fromDayOfWeek"] <= when.weekday() <= period["toDayOfWeek"]:
                start = time_on(when, period["fromHour"], period["fromMinute"])
                end = time_on(when, period["toHour"], period["toMinute"])
                end = ensure_greater(end, start)
                if start <= when < end:
                    return (period_name, Period(**period))
                # else:
                #     logger.warning("not the time %r <= %r <= %r", start, when, end)
            # else:
            #     logger.warning(
            #         "not the day of week %r <= %r <= %r",
            #         period["fromDayOfWeek"],
            #         when.weekday(),
            #         period["toDayOfWeek"],
            #     )
        logger.warning("No period found for season date: %r %s", season_name, when)
        return (None, {})

    def get_period_end(
        self, when: datetime, season_name: SeasonName | None = None
    ) -> datetime:
        """Returns the end time of the time-of-use period for a given date."""
        if not season_name:
            season_name = self.season(when)
        _, period = self.get_period(season_name, when)
        # logger.warning(
        #     "get_period_end() season:%r period_name:%r period:%r",
        #     season_name,
        #     period_name,
        #     period,
        # )
        period_end = time_on(when, period["toHour"], period["toMinute"])
        return ensure_greater(period_end, when)

    def get_rate(self, season_name: SeasonName, period_name: PeriodName) -> float:
        """Returns the energy charge for a given season and period."""
        return self.data["energy_charges"][season_name][period_name]

    def find(self, when: datetime) -> tuple[SeasonName, PeriodName, float]:
        """Returns the season, period, and rate for a given date."""
        season, period = self.season_period(when)
        rate = self.get_rate(season, period)
        return (season, period, rate)

    def get_highest_period(
        self, when: datetime, duration: timedelta
    ) -> tuple[PeriodName, Period, float]:
        """Returns the highest time-of-use period for a given date and duration."""
        highest_period_name = None
        highest_period = None
        highest_rate = None

        while True:
            season_name = self.season(when)
            period_name, period = self.get_period(season_name, when)
            try:
                period_end = ensure_greater(
                    time_on(when, period["toHour"], period["toMinute"]), when
                )
            except KeyError:
                print("ERROR period is missing a key", repr(period))
                raise
            rate = self.get_rate(season_name, period_name)

            if (
                highest_period is None
                or highest_period_name is None
                or highest_rate is None
                or rate > highest_rate
            ):
                highest_period = period
                highest_period_name = period_name
                highest_rate = rate

            duration -= period_end - when
            # logger.warning("remaining duration to check for highest rate: %r", duration)
            when = period_end

            if duration.total_seconds() <= 0:
                break

        return highest_period_name, Period(**highest_period), highest_rate

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
        when.year, when.month, when.day, hour, minute, tzinfo=DEFAULT_TIMEZONE
    )


def ensure_greater_year(value: date, reference: date) -> date:
    """Ensures the given datetime is greater than the reference datetime.
    If it is not, it will be adjusted to the next year."""
    if value < reference:
        # logger.warning("Ensuring greater: %r <= %r", value, reference)
        return date(value.year + 1, value.month, value.day)
    return value


def ensure_greater(value: datetime, reference: datetime) -> datetime:
    """Ensures the given datetime is greater than the reference datetime.
    If it is not, it will be adjusted to the next day."""
    if value < reference:
        # logger.warning("Ensuring greater: %r <= %r", value, reference)
        return value + timedelta(days=1)
    return value


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf8") as fd:
        inputdata = json.load(fd)

    tp = TariffParser(inputdata)
    tps = tp.for_selling()

    for what, tariffs in [('selling', tps), ('buying', tp)]:
        print()
        print(what, tariffs)
        when = datetime.now(tz=DEFAULT_TIMEZONE)
        print(when)
        season = tariffs.season(when)
        print('season', season)
        period_name, _ = tariffs.get_period(season, when)
        print('period name', period_name)
        period_end = tariffs.get_period_end(when, season)
        print('period end', period_end)
        next_season, next_period, next_rate = tariffs.find(
            period_end + timedelta(seconds=1)
        )
        print('next')
        print('season', next_season, 'period', next_period, 'rate', next_rate)

    # threshold = 0.55

    # for mon in [5, 6, 7, 8, 9, 10]:
    #     this_month = date(2025, mon, 1)
    #     next_mon = (this_month + timedelta(days=32)).month
    #     days = (date(2025, next_mon, 1) - this_month).days
    #     for d in range(1, days + 1):
    #         num_hours = 0
    #         start_hour = None
    #         rates = []

    #         for h in range(24):
    #             whenh = datetime(2025, mon, d, h, 1, 0, tzinfo=DEFAULT_TIMEZONE)
    #             _, _, sellrate = tps.find(whenh)
    #             if sellrate >= threshold:
    #                 if start_hour is None:
    #                     start_hour = h
    #                 num_hours += 1
    #                 rates.append(sellrate)

    #         if num_hours > 0:
    #             print(
    #                 f"2025-{mon}-{d} {start_hour}:00 +{num_hours}hr {",".join(map(str, rates))}"
    #             )
