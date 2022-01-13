from __future__ import annotations

from datetime import datetime
from functools import cached_property
from typing import Optional, List, Dict, TYPE_CHECKING

from constants import JsonType, GQL_OPERATIONS

if TYPE_CHECKING:
    from twitch import Twitch


class Game:
    def __init__(self, data: JsonType):
        self.id: int = int(data["id"])
        self.name: str = data["name"]

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Game({self.id}, {self.name})"

    def __eq__(self, other: object):
        if isinstance(other, self.__class__):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.__class__.__name__, self.id))


class BaseDrop:
    def __init__(self, campaign: DropsCampaign, data: JsonType):
        self._twitch: Twitch = campaign._twitch
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.campaign: DropsCampaign = campaign
        self.rewards: List[str] = [b["benefit"]["name"] for b in data["benefitEdges"]]
        self.starts_at: datetime = datetime.strptime(data["startAt"], "%Y-%m-%dT%H:%M:%SZ")
        self.ends_at: datetime = datetime.strptime(data["endAt"], "%Y-%m-%dT%H:%M:%SZ")
        # If claim_id is not None, we can use it to claim the drop
        self.claim_id: Optional[str] = data["self"]["dropInstanceID"]
        self.is_claimed: bool = data["self"]["isClaimed"]
        self._precondition_drops: List[str] = [d["id"] for d in (data["preconditionDrops"] or [])]

    @cached_property
    def preconditions(self) -> bool:
        campaign = self.campaign
        return all(campaign.timed_drops[pid].is_claimed for pid in self._precondition_drops)

    @property
    def can_earn(self) -> bool:
        return (
            self.preconditions  # preconditions are met
            and not self.is_claimed  # drop isn't already claimed
            and self.campaign.active  # campaign is active
            and self.starts_at <= datetime.utcnow() < self.ends_at  # it's within the timeframe
        )

    @property
    def can_claim(self) -> bool:
        return self.claim_id is not None

    def _on_claim(self) -> None:
        del self.preconditions

    def update_claim(self, claim_id: str):
        self.claim_id = claim_id

    def rewards_text(self, delim: str = ", ") -> str:
        return delim.join(self.rewards)

    async def claim(self) -> bool:
        result = await self._claim()
        if result:
            self.is_claimed = True
            # notify the campaign about claiming
            # this will cause it to call our _on_claim, so no need to call it ourselves here
            self.campaign._on_claim()
        return result

    async def _claim(self) -> bool:
        """
        Returns True if the claim succeeded, False otherwise.
        """
        if not self.can_claim:
            return False
        if self.is_claimed:
            return True
        response = await self._twitch.gql_request(
            GQL_OPERATIONS["ClaimDrop"].with_variables(
                {"input": {"dropInstanceID": self.claim_id}}
            )
        )
        data = response["data"]
        if "errors" in data and data["errors"]:
            return False
        elif "claimDropRewards" in data:
            if not data["claimDropRewards"]:
                return False
            elif (
                data["claimDropRewards"]["status"]
                in ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"]
            ):
                return True
        return False


class TimedDrop(BaseDrop):
    def __init__(self, campaign: DropsCampaign, data: JsonType):
        super().__init__(campaign, data)
        self.current_minutes: int = data["self"]["currentMinutesWatched"]
        self.required_minutes: int = data["requiredMinutesWatched"]
        if self.is_claimed:
            # claimed drops report 0 current minutes, so we need to make a correction
            self.current_minutes = self.required_minutes

    @cached_property
    def remaining_minutes(self) -> int:
        return self.required_minutes - self.current_minutes

    @cached_property
    def progress(self) -> float:
        return self.current_minutes / self.required_minutes

    def _on_minutes_changed(self) -> None:
        del self.progress
        del self.remaining_minutes
        self.campaign._on_minutes_changed()

    async def claim(self) -> bool:
        result = await super().claim()
        if result:
            self.current_minutes = self.required_minutes
        return result

    def update_minutes(self, minutes: int):
        self.current_minutes = minutes
        self._on_minutes_changed()

    def display(self, *, countdown: bool = True):
        self.campaign._twitch.gui.progress.display(self, countdown=countdown)

    def bump_minutes(self):
        if self.current_minutes < self.required_minutes:
            self.current_minutes += 1
            self._on_minutes_changed()


class DropsCampaign:
    def __init__(self, twitch: Twitch, data: JsonType):
        self._twitch: Twitch = twitch
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.game: Game = Game(data["game"])
        self.starts_at: datetime = datetime.strptime(data["startAt"], "%Y-%m-%dT%H:%M:%SZ")
        self.ends_at: datetime = datetime.strptime(data["endAt"], "%Y-%m-%dT%H:%M:%SZ")
        allowed = data["allow"]
        self.allowed_channels: List[str] = []
        if allowed["channels"] is not None:
            self.allowed_channels.extend(ch["name"] for ch in allowed["channels"])
        self.timed_drops: Dict[str, TimedDrop] = {
            d["id"]: TimedDrop(self, d) for d in data["timeBasedDrops"]
        }

    @property
    def active(self):
        return self.starts_at <= datetime.utcnow() < self.ends_at

    @property
    def upcoming(self) -> bool:
        return datetime.utcnow() < self.starts_at

    @property
    def expired(self) -> bool:
        return self.ends_at <= datetime.utcnow()

    @property
    def total_drops(self) -> int:
        return len(self.timed_drops)

    @cached_property
    def claimed_drops(self) -> int:
        return sum(d.is_claimed for d in self.timed_drops.values())

    @cached_property
    def remaining_drops(self) -> int:
        return sum(not d.is_claimed for d in self.timed_drops.values())

    @cached_property
    def remaining_minutes(self) -> int:
        return sum(d.remaining_minutes for d in self.timed_drops.values())

    @cached_property
    def progress(self) -> float:
        return sum(d.progress for d in self.timed_drops.values()) / self.total_drops

    def _on_claim(self) -> None:
        del self.claimed_drops
        del self.remaining_drops
        for drop in self.timed_drops.values():
            drop._on_claim()

    def _on_minutes_changed(self) -> None:
        del self.progress
        del self.remaining_minutes
