"""Review attempts keep their own immutable submission; actual fees stay unavailable until settlement."""

from copy import deepcopy


class EvidenceSettlement:

    def public(self, r, resource=None):
        value = self.next_handler(EvidenceSettlement).public(r, resource)
        if isinstance(value, dict) and r.get("kind") == "external_correspondence":
            c = self.w.get("service_cases", {}).get(r.get("case_id"), {})
            if c.get("kind") == "guaranteed_net" and isinstance(value.get("receipt"), dict):
                value["receipt"].pop("actual_external_minor", None)
        return value

    def action_evidence_periods(self, c, a, d):
        if a != "submit":
            return self.next_handler(EvidenceSettlement).action_evidence_periods(c, a, d)
        self.args(d, "record_ids")
        ids = d["record_ids"]
        self.check(isinstance(ids, list) and len(ids) == len(set(ids)), "INVALID_DOCUMENT_SET")
        rs = [self.local(c, x, "statement_offer") for x in ids]
        self.check(all(x["acquired"] for x in rs), "STATEMENT_NOT_AVAILABLE")
        review = self.new(
            "service_reviews",
            case_id=c["id"],
            kind="statement_review",
            document_ids=deepcopy(ids),
            status="PENDING",
            submitted_minute=self.w["minute"],
        )
        c["state"].update(
            submitted_ids=deepcopy(ids),
            current_review_id=review["id"],
            accepted=False,
            review_status="PENDING",
        )
        self.schedule("extension_evidence_review", review["id"], 1)
        return dict(review_id=review["id"], status="PENDING")

    def apply_event(self, e):
        if e.get("kind") != "extension_evidence_review":
            return self.next_handler(EvidenceSettlement).apply_event(e)
        review = self.w["service_reviews"][e["target_id"]]
        c = self.w["service_cases"][review["case_id"]]
        p = self.institution_facts(c)
        rs = [self.w["service_records"][rid] for rid in review["document_ids"]]
        coverage = all(
            any(x["entity"] == span["entity"] and x["start"] <= day <= x["end"] for x in rs)
            for span in p["entity_periods"]
            for day in range(span["start"], span["end"] + 1)
        )
        valid = not p.get("strict_subject_period") or all(
            any(
                x["entity"] == span["entity"]
                and span["start"] <= x["start"] <= x["end"] <= span["end"]
                for span in p["entity_periods"]
            )
            for x in rs
        )
        review.update(
            status="ACCEPTED" if coverage and valid else "REJECTED", decided_minute=self.w["minute"]
        )
        if c["state"]["current_review_id"] == review["id"]:
            c["state"].update(
                accepted=review["status"] == "ACCEPTED", review_status=review["status"]
            )
