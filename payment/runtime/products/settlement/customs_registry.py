from payment.runtime.products.settlement.models import Mechanism

REGISTRY = (
    Mechanism(
        "bonded_drawback",
        "propose_bonded_drawback",
        "Prepare an authorized instruction under the bonded_drawback service contract. Read its public records and complete SOP.",
        "customs_bonded",
        "BondedOperations",
        "customs_instruction",
        "bonded_drawback_event",
        "customs_event",
    ),
    Mechanism(
        "missing_original_loi",
        "propose_missing_original_loi",
        "Prepare an authorized instruction under the missing_original_loi service contract. Read its public records and complete SOP.",
        "customs_loi",
        "LOIOperations",
        "customs_instruction",
        "missing_original_loi_event",
        "customs_event",
    ),
    Mechanism(
        "incoterm_claim_settlement",
        "propose_incoterm_claim_settlement",
        "Prepare an authorized instruction under the incoterm_claim_settlement service contract. Read its public records and complete SOP.",
        "customs_incoterm",
        "IncotermOperations",
        "customs_instruction",
        "incoterm_claim_settlement_event",
        "customs_event",
    ),
    Mechanism(
        "temporary_admission_excise",
        "propose_temporary_admission_excise",
        "Prepare an authorized instruction under the temporary_admission_excise service contract. Read its public records and complete SOP.",
        "customs_temporary",
        "TemporaryOperations",
        "customs_instruction",
        "temporary_admission_excise_event",
        "customs_event",
    ),
)
