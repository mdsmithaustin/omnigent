from __future__ import annotations

import itertools
import re
import subprocess
from dataclasses import dataclass

from omnigent.harnesses.prime_native.process import resolve_prime_executable


@dataclass(frozen=True)
class PrimeModelRef:
    provider: str
    model_id: str

    @property
    def selector(self) -> str:
        return f"{self.provider}/{self.model_id}"

    @classmethod
    def parse(cls, selector: str) -> PrimeModelRef:
        provider, separator, model_id = selector.partition("/")
        if not separator or not provider or not model_id or any(c.isspace() for c in selector):
            raise ValueError("Prime model selection requires an exact provider/model pair")
        return cls(provider, model_id)


def parse_model_table(output: str) -> list[PrimeModelRef]:
    lines = output.strip().splitlines()
    empty_message = (
        "No models available. Use /login to log into a provider via OAuth or API key. See:"
    )
    if (
        len(lines) == 3
        and lines[0] == empty_message
        and re.fullmatch(r"  /.+/docs/providers\.md", lines[1])
        and re.fullmatch(r"  /.+/docs/models\.md", lines[2])
    ):
        return []
    headers = ("provider", "model", "context", "max-out", "thinking", "images")
    if not lines or tuple(lines[0].split()) != headers:
        raise ValueError("Unrecognized Prime model catalog header")
    offsets = [lines[0].index(header) for header in headers]
    models: list[PrimeModelRef] = []
    seen: set[str] = set()
    for line in lines[1:]:
        cells = [line[start:end].strip() for start, end in itertools.pairwise(offsets)]
        cells.append(line[offsets[-1] :].strip())
        if (
            len(line.split()) != 6
            or any(not cell or any(c.isspace() for c in cell) for cell in cells)
            or not all(re.fullmatch(r"\d+(?:\.\d+)?[kKmM]?", cell) for cell in cells[2:4])
            or cells[4] not in {"yes", "no"}
            or cells[5] not in {"yes", "no"}
        ):
            raise ValueError("Malformed Prime model catalog row")
        model = PrimeModelRef(cells[0], cells[1])
        if model.selector in seen:
            raise ValueError("Duplicate Prime model selector")
        seen.add(model.selector)
        models.append(model)
    if not models:
        raise ValueError("Prime model catalog has no rows or explicit empty result")
    return models


def model_options() -> list[dict[str, object]]:
    executable = resolve_prime_executable()
    result = subprocess.run(
        [executable, "model", "list"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return [
        {"id": model.selector, "model": model.selector, "displayName": model.selector}
        for model in parse_model_table(result.stdout)
    ]
