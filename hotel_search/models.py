"""Input validation shared by the CLI and the orchestrator."""

from dataclasses import asdict, dataclass
from datetime import date
import re


@dataclass(frozen=True)
class SearchRequest:
    city: str | None = None
    check_in: str | None = None
    check_out: str | None = None
    guests: int = 1
    currency: str = "CAD"
    province: str = "Ontario"
    country: str = "Canada"

    def __post_init__(self):
        if self.city is not None and (not isinstance(self.city, str) or not self.city.strip() or len(self.city) > 120):
            raise ValueError("city must be a nonempty name of at most 120 characters")
        if self.city is not None and any(ord(c) < 32 for c in self.city):
            raise ValueError("city must not contain control characters")
        if self.city is not None:
            object.__setattr__(self, "city", self.city.strip())
        for field in ("check_in", "check_out"):
            value = getattr(self, field)
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{field} must use YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{field} must be a valid calendar date") from None
        if not 1 <= self.nights <= 365:
            raise ValueError("check_out must be 1 to 365 nights after check_in")
        if type(self.guests) is not int or not 1 <= self.guests <= 20:
            raise ValueError("guests must be an integer from 1 to 20 (adults only)")
        if not isinstance(self.currency, str) or self.currency != "CAD":
            raise ValueError("this competition module uses CAD only")
        if self.province != "Ontario" or self.country != "Canada":
            raise ValueError("this competition module searches Ontario, Canada")

    @property
    def nights(self):
        return (date.fromisoformat(self.check_out) - date.fromisoformat(self.check_in)).days

    def to_dict(self):
        return {**asdict(self), "nights": self.nights, "scope": self.scope}

    @property
    def scope(self):
        return "city" if self.city else "province"

    @property
    def search_location(self):
        return ", ".join(part for part in (self.city, self.province, self.country) if part)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("request must be a JSON object")
        allowed = set(cls.__dataclass_fields__)
        extra = set(value) - allowed
        if extra:
            raise ValueError("unsupported request fields: " + ", ".join(sorted(extra)))
        missing = {"check_in", "check_out"} - set(value)
        if missing:
            raise ValueError("missing request fields: " + ", ".join(sorted(missing)))
        return cls(**value)
