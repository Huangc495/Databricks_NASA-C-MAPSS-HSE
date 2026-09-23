"""Optional FD001 incremental quality probe; never uploaded by normal preparation."""
from pathlib import Path


def probe_rows(raw_line: str) -> list[str]:
    tokens = raw_line.split()
    if len(tokens) != 26:
        raise ValueError("Expected a complete C-MAPSS observation")
    def changed(index, value):
        row = tokens.copy()
        row[index] = value
        return " ".join(row)
    conflict = tokens.copy()
    conflict[0] = "999"
    conflict2 = conflict.copy()
    conflict2[-1] = str(float(conflict2[-1]) + 1)
    return [raw_line.strip(), "   ".join(tokens), "1 2 3",
            changed(2, "invalid"), changed(3, "NaN"),
            changed(0, "0"), changed(1, "1.5"),
            " ".join(conflict), " ".join(conflict2)]


if __name__ == "__main__":
    first = Path("data/cmapss/train_FD001.txt").read_text().splitlines()[0]
    target = Path("data/quality_probe/train_FD001_quality.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(probe_rows(first)) + "\n")
