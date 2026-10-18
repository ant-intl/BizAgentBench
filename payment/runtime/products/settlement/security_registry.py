"""Security service capability definitions."""

REGISTRY = [
    dict(
        kind="credential_ack_cutover",
        tool_name="propose_credential_cutover",
        tool_description="Prepare a scoped sender fence, finite-capacity credential activation, original-payment retry, capacity-limited standard or paid priority supplier remittance, or production cutover.",
        implementation_prefix="security_credential",
        operation_class="CredentialOperations",
        operation_method="credential_instruction",
        event_kind="credential_ack_cutover_event",
        event_method="credential_event",
    ),
    dict(
        kind="traced_fraud_restitution",
        tool_name="propose_traced_fraud_recall",
        tool_description="Prepare a mixed-account freeze, instant or deferred original-edge fraud return, final settlement election or legitimate supplier payment.",
        implementation_prefix="security_fraud",
        operation_class="FraudOperations",
        operation_method="fraud_instruction",
        event_kind="traced_fraud_restitution_event",
        event_method="fraud_event",
    ),
    dict(
        kind="claim_field_retention",
        tool_name="propose_claim_retention",
        tool_description="Prepare a original-evidence secured advance, collateral substitution, repayment, claim settlement, evidence export, claim submission, original deletion or replacement-goods payment.",
        implementation_prefix="security_retention",
        operation_class="RetentionOperations",
        operation_method="retention_instruction",
        event_kind="claim_field_retention_event",
        event_method="retention_event",
    ),
]
