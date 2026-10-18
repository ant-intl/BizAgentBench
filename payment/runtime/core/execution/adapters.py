"""Product adapters behind the single Payment execution protocol.

The benchmark has two product portals with different business objects and
state-transition libraries.  Those differences belong here.  The public
runtime never selects a second execution protocol: it validates, rolls back
and audits every call once, then delegates only the business effect.
"""

from __future__ import annotations

OPERATIONS_DESK = "operations_desk"
PSP = "psp"


class CanonicalRequestStore:
    """Logical request store over the two frozen physical table names.

    Existing releases persist ``requests`` and ``xp_requests``.  Renaming
    either table would change frozen worlds and verifier inputs, so the unified
    runtime treats them as compatibility views of one request abstraction.
    """

    def __init__(self, world, physical_name):
        self.world = world
        self.physical_name = physical_name

    @property
    def rows(self):
        return self.world.setdefault(self.physical_name, {})

    def contains(self, request_id):
        return request_id in self.rows


class ProductAdapter:
    product_scope = None
    request_table = None

    def __init__(self, world, operations, documents, event_handlers):
        self.world = world
        self.operations = operations
        self.documents = documents
        self.event_handlers = event_handlers
        self.requests = CanonicalRequestStore(world, self.request_table)

    def before_call(self):
        pass

    def after_call(self, name):
        pass

    def is_closed(self):
        raise NotImplementedError

    def invoke_customer_action(self, name, **arguments):
        method = getattr(self.native, name, None)
        if method is None:
            from payment.runtime.products.psp.kernel import BusinessError

            raise BusinessError("TOOL_NOT_AVAILABLE")
        return method(**arguments)

    def call_capability(self, name, arguments):
        return self.native.call_discoverable_agent_tool(name, arguments)

    def decide_request(self, request_id, decision):
        if not self.requests.contains(request_id):
            from payment.runtime.products.psp.kernel import BusinessError

            raise BusinessError("RECORD_NOT_FOUND")
        return self.native.decide_request(request_id, decision)

    def execute_request(self, request_id):
        if not self.requests.contains(request_id):
            from payment.runtime.products.psp.kernel import BusinessError

            raise BusinessError("RECORD_NOT_FOUND")
        return self.native.execute_request(request_id)

    def get_clock(self):
        return self.native.get_clock()

    def get_my_requests(self):
        return self.native.get_my_requests()

    def list_my_files(self):
        return self.native.list_my_files()

    def record_case_outcome(self, outcome, summary):
        return self.native.record_case_outcome(outcome, summary)

    def upload_file(self, file_id, target_id, purpose):
        return self.native.upload_file(file_id, target_id, purpose)

    def wait_for_update(self):
        return self.native.wait_for_update()


class OperationsDeskAdapter(ProductAdapter):
    product_scope = OPERATIONS_DESK
    request_table = "requests"

    def __init__(self, world, operations, documents, event_handlers):
        super().__init__(world, operations, documents, event_handlers)
        from payment.runtime.core.operations.engine import Engine

        self.native = Engine(world)

    def before_call(self):
        if hasattr(self.native, "refresh_programmes"):
            self.native.refresh_programmes()

    def after_call(self, name):
        if name == "record_case_outcome":
            return
        if hasattr(self.native, "refresh_programmes"):
            self.native.refresh_programmes()

        # Joint funding terms become observable after the first real booking
        # in the assessment service.  This is a product lifecycle effect, not
        # a second public execution path.
        for agreement in self.world.get("joint_agreements", {}).values():
            if (
                agreement.get("scope") != self.world.get("_session_scope")
                or agreement.get("assessment_status") == "READY"
            ):
                continue
            bookings = [
                booking
                for booking in self.world.get("service_bookings", {}).values()
                if booking.get("case_id") == agreement.get("assessment_case_id")
                and booking.get("contract")
                == self.world.get("service_cases", {})
                .get(agreement.get("assessment_case_id"), {})
                .get("kind")
                and booking.get("kind") != "bank_event"
            ]
            if not bookings:
                continue
            first = min(bookings, key=lambda booking: (booking["minute"], booking["id"]))
            agreement.update(
                assessment_status="READY",
                assessment_booking_id=first["id"],
                assessment_seq=len(self.world["audit"]),
                terms_version=2,
            )
            for route in self.world.get("joint_routes", {}).values():
                if route.get("agreement_id") == agreement["id"] and route.get(
                    "case_id"
                ) != agreement.get("assessment_case_id"):
                    route.update(fee_minor=route["_assessed_fee_minor"], terms_version=2)

        from payment.runtime.products.joint_funding.milestones import satisfied

        for source in self.world.get("joint_sources", {}).values():
            if (
                source.get("scope") == self.world.get("_session_scope")
                and source.get("status") == "RESTRICTED"
                and satisfied(self.world, source["release_condition"])
            ):
                source["status"] = "AVAILABLE"

    def is_closed(self):
        return bool(self.world.get("case_outcome"))

    def search_records(self, resource, filters, cursor, limit):
        if resource.startswith("xp_"):
            from payment.runtime.products.psp.kernel import BusinessError

            raise BusinessError("PRODUCT_NOT_ENABLED_FOR_CURRENT_PORTAL")
        return self.native.search_records(resource, filters, cursor, limit)

    def get_record(self, resource, record_id):
        if resource.startswith("xp_"):
            from payment.runtime.products.psp.kernel import BusinessError

            raise BusinessError("PRODUCT_NOT_ENABLED_FOR_CURRENT_PORTAL")
        return self.native.get_record(resource, record_id)


class PSPAdapter(ProductAdapter):
    product_scope = PSP
    request_table = "xp_requests"

    def __init__(self, world, operations, documents, event_handlers):
        super().__init__(world, operations, documents, event_handlers)
        from payment.runtime.products.psp.kernel import Engine

        self.native = Engine(world, operations, documents, event_handlers)

    def is_closed(self):
        return bool(self.world.get("xp_outcomes"))

    def search_records(self, resource, filters, cursor, limit):
        if not resource.startswith("xp_"):
            from payment.runtime.products.psp.kernel import BusinessError

            raise BusinessError("PRODUCT_NOT_ENABLED_FOR_CURRENT_PORTAL")
        return self.native.search_records(resource, filters, cursor, limit)

    def get_record(self, resource, record_id):
        if not resource.startswith("xp_"):
            from payment.runtime.products.psp.kernel import BusinessError

            raise BusinessError("PRODUCT_NOT_ENABLED_FOR_CURRENT_PORTAL")
        return self.native.get_record(resource, record_id)


def make_adapter(world, operations, documents, event_handlers):
    cls = PSPAdapter if world.get("_payment_expansion") else OperationsDeskAdapter
    return cls(world, operations, documents, event_handlers)
