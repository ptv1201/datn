"""Offline evaluation on user-labelled examples; no demo score is hard-coded."""
from __future__ import annotations

import json
from collections.abc import Iterable

from core import FIELDS, extract_post_llm, extract_rules, key, normalize
from storage import find_similar


def prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"TP": tp, "FP": fp, "FN": fn, "Precision": round(p, 4),
            "Recall": round(r, 4), "F1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def evaluate_extraction(rows: Iterable[dict]) -> dict:
    total = [0, 0, 0]
    per_field = {}
    rows = list(rows)
    for field in FIELDS:
        tp = fp = fn = 0
        for row in rows:
            predicted = extract_rules(str(row.get("raw_text", ""))).facts().get(field, "").strip().casefold()
            expected = str(row.get(field, "") or "").strip().casefold()
            if predicted and predicted == expected:
                tp += 1
            else:
                fp += bool(predicted)
                fn += bool(expected)
        per_field[field] = prf(tp, fp, fn)
        for i, value in enumerate((tp, fp, fn)):
            total[i] += value
    return {"micro": prf(*total), "fields": per_field, "rows": len(rows)}


def evaluate_llm_extraction(rows: Iterable[dict]) -> dict:
    """Compare person+field+value triples; one row represents one whole post."""
    per_field = {field: [0, 0, 0] for field in FIELDS}
    row_count = 0
    for row in rows:
        expected_people = json.loads(row["expected_people_json"])
        if not isinstance(expected_people, list):
            raise ValueError("expected_people_json phải là danh sách JSON")
        prediction = extract_post_llm(str(row["raw_text"])).people
        expected_set, predicted_set = set(), set()
        for person in expected_people:
            if not isinstance(person, dict):
                raise ValueError("Mỗi cá nhân trong nhãn vàng phải là một object")
            person_id = key(person.get("identifier") or person.get("full_name"))
            if not person_id:
                raise ValueError("Cá nhân trong nhãn vàng cần họ tên hoặc mã")
            for field in FIELDS:
                if str(person.get(field) or "").strip():
                    expected_set.add((person_id, field, str(person[field]).strip().casefold()))
        for person in prediction:
            fields = person.facts.facts()
            person_id = key(fields.get("identifier") or fields.get("full_name"))
            for field, value in fields.items():
                if value.strip():
                    predicted_set.add((person_id, field, value.strip().casefold()))
        for entry in expected_set & predicted_set:
            per_field[entry[1]][0] += 1
        for entry in predicted_set - expected_set:
            per_field[entry[1]][1] += 1
        for entry in expected_set - predicted_set:
            per_field[entry[1]][2] += 1
        row_count += 1
    totals = [sum(values[index] for values in per_field.values()) for index in range(3)]
    return {"micro": prf(*totals), "fields": {field: prf(*counts) for field, counts in per_field.items()},
            "posts": row_count}


def evaluate_normalization(rows: Iterable[dict]) -> dict:
    correct = count = 0
    for row in rows:
        field = str(row.get("field", ""))
        if field not in FIELDS:
            continue
        count += 1
        correct += normalize(field, str(row.get("raw_value", "")))[0] == str(row.get("expected", ""))
    return {"correct": correct, "total": count, "Accuracy": round(correct / count, 4) if count else 0.0}


def evaluate_pairs(rows: Iterable[dict], threshold: float = .78) -> dict:
    """Pair labels are from human annotation; classifier uses actual candidate blocking."""
    tp = fp = fn = 0
    for row in rows:
        a, b = int(row["record_a"]), int(row["record_b"])
        truth = str(row["is_duplicate"]).strip().lower() in {"1", "true", "yes"}
        predicted = any(item["id"] == b and item["score"] >= threshold
                        for item in find_similar(a, limit=300))
        tp += predicted and truth
        fp += predicted and not truth
        fn += (not predicted) and truth
    return prf(tp, fp, fn)


def evaluate_ranking(rows: Iterable[dict], k: int = 5) -> dict:
    p_values, r_values, hit_values = [], [], []
    for row in rows:
        relevant = {int(v) for v in str(row["relevant_ids"]).replace("|", ",").split(",") if v.strip()}
        if not relevant:
            continue
        predicted = {item["id"] for item in find_similar(int(row["query_id"]), limit=k)[:k]}
        hits = len(predicted & relevant)
        p_values.append(hits / k)
        r_values.append(hits / len(relevant))
        hit_values.append(float(hits > 0))
    n = len(p_values)
    return {"queries": n, f"Precision@{k}": round(sum(p_values) / n, 4) if n else 0,
            f"Recall@{k}": round(sum(r_values) / n, 4) if n else 0,
            f"Top-{k} Accuracy": round(sum(hit_values) / n, 4) if n else 0}
