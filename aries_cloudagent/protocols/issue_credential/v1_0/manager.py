"""Classes to manage credentials."""

import asyncio
import json
import logging

from typing import Mapping, Sequence, Text, Tuple

from .messages.credential_ack import CredentialAck
from .messages.credential_issue import CredentialIssue
from .messages.credential_offer import CredentialOffer
from .messages.credential_proposal import CredentialProposal
from .messages.credential_request import CredentialRequest
from .messages.inner.credential_preview import CredentialPreview
from .models.credential_exchange import V10CredentialExchange
from ....cache.base import BaseCache
from ....config.injection_context import InjectionContext
from ....core.error import BaseError
from ....holder.base import BaseHolder, HolderError
from ....issuer.base import BaseIssuer
from ....issuer.indy import IssuerRevocationRegistryFullError
from ....ledger.base import BaseLedger
from ....messaging.credential_definitions.util import (
    CRED_DEF_TAGS,
    CRED_DEF_SENT_RECORD_TYPE,
)
from ....revocation.indy import IndyRevocation
from ....revocation.models.revocation_registry import RevocationRegistry
from ....revocation.models.issuer_rev_reg_record import IssuerRevRegRecord
from ....storage.base import BaseStorage
from ....storage.error import StorageNotFoundError

from ....messaging.util import time_now
from ....utils.frill import Ink


class CredentialManagerError(BaseError):
    """Credential error."""


class CredentialManager:
    """Class for managing credentials."""

    def __init__(self, context: InjectionContext):
        """
        Initialize a CredentialManager.

        Args:
            context: The context for this credential
        """
        self._context = context
        self._logger = logging.getLogger(__name__)

    @property
    def context(self) -> InjectionContext:
        """
        Accessor for the current request context.

        Returns:
            The request context for this connection

        """
        return self._context

    async def _match_sent_cred_def_id(self, tag_query: Mapping[str, str]) -> str:
        """Return most recent matching id of cred def that agent sent to ledger."""

        storage: BaseStorage = await self.context.inject(BaseStorage)
        found = await storage.search_records(
            type_filter=CRED_DEF_SENT_RECORD_TYPE, tag_query=tag_query
        ).fetch_all()
        if not found:
            raise CredentialManagerError(
                f"Issuer has no operable cred def for proposal spec {tag_query}"
            )
        return max(found, key=lambda r: int(r.tags["epoch"])).tags["cred_def_id"]

    async def prepare_send(
        self,
        connection_id: str,
        credential_proposal: CredentialProposal,
        auto_remove: bool = None,
    ) -> Tuple[V10CredentialExchange, CredentialOffer]:
        """
        Set up a new credential exchange for an automated send.

        Args:
            connection_id: Connection to create offer for
            credential_proposal: The credential proposal with preview
            auto_remove: Flag to automatically remove the record on completion

        Returns:
            A tuple of the new credential exchange record and credential offer message

        """
        if auto_remove is None:
            auto_remove = not self.context.settings.get("preserve_exchange_records")
        credential_exchange = V10CredentialExchange(
            auto_issue=True,
            auto_remove=auto_remove,
            connection_id=connection_id,
            initiator=V10CredentialExchange.INITIATOR_SELF,
            role=V10CredentialExchange.ROLE_ISSUER,
            credential_proposal_dict=credential_proposal.serialize(),
            trace=(credential_proposal._trace is not None),
        )
        (credential_exchange, credential_offer) = await self.create_offer(
            cred_ex_record=credential_exchange,
            comment="create automated credential exchange",
        )
        return (credential_exchange, credential_offer)

    async def create_proposal(
        self,
        connection_id: str,
        *,
        auto_offer: bool = None,
        auto_remove: bool = None,
        comment: str = None,
        credential_preview: CredentialPreview = None,
        schema_id: str = None,
        schema_issuer_did: str = None,
        schema_name: str = None,
        schema_version: str = None,
        cred_def_id: str = None,
        issuer_did: str = None,
        trace: bool = False,
    ) -> V10CredentialExchange:
        """
        Create a credential proposal.

        Args:
            connection_id: Connection to create proposal for
            auto_offer: Should this proposal request automatically be handled to
                offer a credential
            auto_remove: Should the record be automatically removed on completion
            comment: Optional human-readable comment to include in proposal
            credential_preview: The credential preview to use to create
                the credential proposal
            schema_id: Schema id for credential proposal
            schema_issuer_did: Schema issuer DID for credential proposal
            schema_name: Schema name for credential proposal
            schema_version: Schema version for credential proposal
            cred_def_id: Credential definition id for credential proposal
            issuer_did: Issuer DID for credential proposal

        Returns:
            Resulting credential exchange record including credential proposal

        """
        credential_proposal_message = CredentialProposal(
            comment=comment,
            credential_proposal=credential_preview,
            schema_id=schema_id,
            schema_issuer_did=schema_issuer_did,
            schema_name=schema_name,
            schema_version=schema_version,
            cred_def_id=cred_def_id,
            issuer_did=issuer_did,
        )
        credential_proposal_message.assign_trace_decorator(self.context.settings, trace)

        if auto_remove is None:
            auto_remove = not self.context.settings.get("preserve_exchange_records")
        cred_ex_record = V10CredentialExchange(
            connection_id=connection_id,
            thread_id=credential_proposal_message._thread_id,
            initiator=V10CredentialExchange.INITIATOR_SELF,
            role=V10CredentialExchange.ROLE_HOLDER,
            state=V10CredentialExchange.STATE_PROPOSAL_SENT,
            credential_proposal_dict=credential_proposal_message.serialize(),
            auto_offer=auto_offer,
            auto_remove=auto_remove,
            trace=trace,
        )
        await cred_ex_record.save(self.context, reason="create credential proposal")
        return cred_ex_record

    async def receive_proposal(self) -> V10CredentialExchange:
        """
        Receive a credential proposal from message in context on manager creation.

        Returns:
            The resulting credential exchange record, created

        """
        credential_proposal_message = self.context.message
        connection_id = self.context.connection_record.connection_id

        # at this point, cred def and schema still open to potential negotiation
        cred_ex_record = V10CredentialExchange(
            connection_id=connection_id,
            thread_id=credential_proposal_message._thread_id,
            initiator=V10CredentialExchange.INITIATOR_EXTERNAL,
            role=V10CredentialExchange.ROLE_ISSUER,
            state=V10CredentialExchange.STATE_PROPOSAL_RECEIVED,
            credential_proposal_dict=credential_proposal_message.serialize(),
            auto_offer=self.context.settings.get(
                "debug.auto_respond_credential_proposal"
            ),
            auto_issue=self.context.settings.get(
                "debug.auto_respond_credential_request"
            ),
            trace=(credential_proposal_message._trace is not None),
        )
        await cred_ex_record.save(self.context, reason="receive credential proposal")

        return cred_ex_record

    async def create_offer(
        self, cred_ex_record: V10CredentialExchange, comment: str = None
    ) -> Tuple[V10CredentialExchange, CredentialOffer]:
        """
        Create a credential offer, update credential exchange record.

        Args:
            cred_ex_record: Credential exchange to create offer for
            comment: optional human-readable comment to set in offer message

        Returns:
            A tuple (credential exchange record, credential offer message)

        """

        async def _create(cred_def_id):
            issuer: BaseIssuer = await self.context.inject(BaseIssuer)
            offer_json = await issuer.create_credential_offer(cred_def_id)
            return json.loads(offer_json)

        credential_proposal_message = CredentialProposal.deserialize(
            cred_ex_record.credential_proposal_dict
        )
        credential_proposal_message.assign_trace_decorator(
            self.context.settings, cred_ex_record.trace
        )
        cred_def_id = await self._match_sent_cred_def_id(
            {
                t: getattr(credential_proposal_message, t)
                for t in CRED_DEF_TAGS
                if getattr(credential_proposal_message, t)
            }
        )
        cred_preview = credential_proposal_message.credential_proposal

        # vet attributes
        ledger: BaseLedger = await self.context.inject(BaseLedger)
        async with ledger:
            schema_id = await ledger.credential_definition_id2schema_id(cred_def_id)
            schema = await ledger.get_schema(schema_id)
        schema_attrs = {attr for attr in schema["attrNames"]}
        preview_attrs = {attr for attr in cred_preview.attr_dict()}
        if preview_attrs != schema_attrs:
            raise CredentialManagerError(
                f"Preview attributes {preview_attrs} "
                f"mismatch corresponding schema attributes {schema_attrs}"
            )

        credential_offer = None
        cache_key = f"credential_offer::{cred_def_id}"
        cache: BaseCache = await self.context.inject(BaseCache, required=False)
        if cache:
            async with cache.acquire(cache_key) as entry:
                if entry.result:
                    credential_offer = entry.result
                else:
                    credential_offer = await _create(cred_def_id)
                    await entry.set_result(credential_offer, 3600)
        if not credential_offer:
            credential_offer = await _create(cred_def_id)

        credential_offer_message = CredentialOffer(
            comment=comment,
            credential_preview=cred_preview,
            offers_attach=[CredentialOffer.wrap_indy_offer(credential_offer)],
        )

        credential_offer_message._thread = {"thid": cred_ex_record.thread_id}
        credential_offer_message.assign_trace_decorator(
            self.context.settings, cred_ex_record.trace
        )

        cred_ex_record.thread_id = credential_offer_message._thread_id
        cred_ex_record.schema_id = credential_offer["schema_id"]
        cred_ex_record.credential_definition_id = credential_offer["cred_def_id"]
        cred_ex_record.state = V10CredentialExchange.STATE_OFFER_SENT
        cred_ex_record.credential_offer = credential_offer

        cred_ex_record.credential_offer_dict = credential_offer_message.serialize()

        await cred_ex_record.save(self.context, reason="create credential offer")

        return (cred_ex_record, credential_offer_message)

    async def receive_offer(self) -> V10CredentialExchange:
        """
        Receive a credential offer.

        Returns:
            The credential exchange record, updated

        """
        credential_offer_message: CredentialOffer = self.context.message
        connection_id = self.context.connection_record.connection_id

        credential_preview = credential_offer_message.credential_preview
        indy_offer = credential_offer_message.indy_offer(0)
        schema_id = indy_offer["schema_id"]
        cred_def_id = indy_offer["cred_def_id"]

        credential_proposal_dict = CredentialProposal(
            comment=credential_offer_message.comment,
            credential_proposal=credential_preview,
            schema_id=schema_id,
            cred_def_id=cred_def_id,
        ).serialize()

        # Get credential exchange record (holder sent proposal first)
        # or create it (issuer sent offer first)
        try:
            (
                cred_ex_record
            ) = await V10CredentialExchange.retrieve_by_connection_and_thread(
                self.context, connection_id, credential_offer_message._thread_id
            )
            cred_ex_record.credential_proposal_dict = credential_proposal_dict
        except StorageNotFoundError:  # issuer sent this offer free of any proposal
            cred_ex_record = V10CredentialExchange(
                connection_id=connection_id,
                thread_id=credential_offer_message._thread_id,
                initiator=V10CredentialExchange.INITIATOR_EXTERNAL,
                role=V10CredentialExchange.ROLE_HOLDER,
                credential_proposal_dict=credential_proposal_dict,
                trace=(credential_offer_message._trace is not None),
            )

        cred_ex_record.credential_offer = indy_offer
        cred_ex_record.state = V10CredentialExchange.STATE_OFFER_RECEIVED
        cred_ex_record.schema_id = schema_id
        cred_ex_record.credential_definition_id = cred_def_id

        await cred_ex_record.save(self.context, reason="receive credential offer")

        return cred_ex_record

    async def create_request(
        self, cred_ex_record: V10CredentialExchange, holder_did: str
    ) -> Tuple[V10CredentialExchange, CredentialRequest]:
        """
        Create a credential request.

        Args:
            cred_ex_record: Credential exchange record
                for which to create request
            holder_did: holder DID

        Returns:
            A tuple (credential exchange record, credential request message)

        """
        if cred_ex_record.state != V10CredentialExchange.STATE_OFFER_RECEIVED:
            raise CredentialManagerError(
                f"Credential exchange {cred_ex_record.credential_exchange_id} "
                f"in {cred_ex_record.state} state "
                f"(must be {V10CredentialExchange.STATE_OFFER_RECEIVED})"
            )

        credential_definition_id = cred_ex_record.credential_definition_id
        credential_offer = cred_ex_record.credential_offer

        async def _create():
            ledger: BaseLedger = await self.context.inject(BaseLedger)
            async with ledger:
                credential_definition = await ledger.get_credential_definition(
                    credential_definition_id
                )

            holder: BaseHolder = await self.context.inject(BaseHolder)
            request_json, metadata_json = await holder.create_credential_request(
                credential_offer, credential_definition, holder_did
            )
            return {
                "request": json.loads(request_json),
                "metadata": json.loads(metadata_json),
            }

        if cred_ex_record.credential_request:
            self._logger.warning(
                "create_request called multiple times for v1.0 credential exchange: %s",
                cred_ex_record.credential_exchange_id,
            )
        else:
            if "nonce" not in credential_offer:
                raise CredentialManagerError("Missing nonce in credential offer")
            nonce = credential_offer["nonce"]
            cache_key = (
                f"credential_request::{credential_definition_id}::{holder_did}::{nonce}"
            )
            cred_req_result = None
            cache: BaseCache = await self.context.inject(BaseCache, required=False)
            if cache:
                async with cache.acquire(cache_key) as entry:
                    if entry.result:
                        cred_req_result = entry.result
                    else:
                        cred_req_result = await _create()
                        await entry.set_result(cred_req_result, 3600)
            if not cred_req_result:
                cred_req_result = await _create()

            (
                cred_ex_record.credential_request,
                cred_ex_record.credential_request_metadata,
            ) = (cred_req_result["request"], cred_req_result["metadata"])

        credential_request_message = CredentialRequest(
            requests_attach=[
                CredentialRequest.wrap_indy_cred_req(cred_ex_record.credential_request)
            ]
        )
        credential_request_message._thread = {"thid": cred_ex_record.thread_id}
        credential_request_message.assign_trace_decorator(
            self.context.settings, cred_ex_record.trace
        )

        cred_ex_record.state = V10CredentialExchange.STATE_REQUEST_SENT
        await cred_ex_record.save(self.context, reason="create credential request")

        return (cred_ex_record, credential_request_message)

    async def receive_request(self):
        """
        Receive a credential request.

        Args:
            credential_request_message: Credential request to receive

        Returns:
            credential exchange record, retrieved and updated

        """
        credential_request_message = self.context.message
        assert len(credential_request_message.requests_attach or []) == 1
        credential_request = credential_request_message.indy_cred_req(0)
        connection_id = (
            self.context.connection_record
            and self.context.connection_record.connection_id
        )

        (
            cred_ex_record
        ) = await V10CredentialExchange.retrieve_by_connection_and_thread(
            self.context, connection_id, credential_request_message._thread_id
        )
        cred_ex_record.credential_request = credential_request
        cred_ex_record.state = V10CredentialExchange.STATE_REQUEST_RECEIVED
        await cred_ex_record.save(self.context, reason="receive credential request")

        return cred_ex_record

    async def issue_credential(
        self,
        cred_ex_record: V10CredentialExchange,
        *,
        comment: str = None,
        retries: int = 5,
    ) -> Tuple[V10CredentialExchange, CredentialIssue]:
        """
        Issue a credential.

        Args:
            cred_ex_record: The credential exchange record
                for which to issue a credential
            comment: optional human-readable comment pertaining to credential issue

        Returns:
            Tuple: (Updated credential exchange record, credential message)

        """
        print(
            Ink.GREEN(
                "\n\n-- {} -- ISSUE-CRED PROTO MGR issue-cred start, retries={}".format(
                    time_now(), retries
                )
            )
        )
        if cred_ex_record.state != V10CredentialExchange.STATE_REQUEST_RECEIVED:
            raise CredentialManagerError(
                f"Credential exchange {cred_ex_record.credential_exchange_id} "
                f"in {cred_ex_record.state} state "
                f"(must be {V10CredentialExchange.STATE_REQUEST_RECEIVED})"
            )

        schema_id = cred_ex_record.schema_id
        registry = None

        if cred_ex_record.credential:
            self._logger.warning(
                "issue_credential called multiple times for "
                + "v1.0 credential exchange: %s",
                cred_ex_record.credential_exchange_id,
            )
        else:
            credential_offer = cred_ex_record.credential_offer
            credential_request = cred_ex_record.credential_request

            ledger: BaseLedger = await self.context.inject(BaseLedger)
            async with ledger:
                schema = await ledger.get_schema(schema_id)
                credential_definition = await ledger.get_credential_definition(
                    cred_ex_record.credential_definition_id
                )

            tails_path = None
            if credential_definition["value"].get("revocation"):
                staged_rev_regs = await IssuerRevRegRecord.query_by_cred_def_id(
                    self.context,
                    cred_ex_record.credential_definition_id,
                    state=IssuerRevRegRecord.STATE_STAGED,
                )

                if staged_rev_regs and retries > 0:
                    # We know there is a staged registry that will be ready soon.
                    # So we wait and retry.
                    await asyncio.sleep(1)
                    return await self.issue_credential(
                        cred_ex_record=cred_ex_record,
                        comment=comment,
                        retries=retries - 1,
                    )
                else:
                    active_rev_regs = await IssuerRevRegRecord.query_by_cred_def_id(
                        self.context,
                        cred_ex_record.credential_definition_id,
                        state=IssuerRevRegRecord.STATE_ACTIVE,
                    )
                    if not active_rev_regs:
                        raise CredentialManagerError(
                            "Cred def id {} has no active revocation registry".format(
                                cred_ex_record.credential_definition_id
                            )
                        )

                    active_reg = active_rev_regs[0]
                    registry = await active_reg.get_registry()
                    cred_ex_record.revoc_reg_id = active_reg.revoc_reg_id
                    tails_path = registry.tails_local_path
                    await registry.get_or_fetch_local_tails_path()

            credential_values = CredentialProposal.deserialize(
                cred_ex_record.credential_proposal_dict
            ).credential_proposal.attr_dict(decode=False)
            issuer: BaseIssuer = await self.context.inject(BaseIssuer)
            try:
                (
                    credential_json,
                    cred_ex_record.revocation_id,
                ) = await issuer.create_credential(
                    schema,
                    credential_offer,
                    credential_request,
                    credential_values,
                    cred_ex_record.revoc_reg_id,
                    tails_path,
                )

                print(
                    Ink.GREEN(
                        ".. {} issued cred rrid {} crid {}".format(
                            time_now(),
                            cred_ex_record.revoc_reg_id,
                            cred_ex_record.revocation_id,
                        )
                    )
                )
                # If the revocation registry is now full
                if registry and registry.max_creds == int(cred_ex_record.revocation_id):
                    print(
                        Ink.GREEN(
                            ".. {} rr_id {} now full".format(
                                time_now(), cred_ex_record.revoc_reg_id
                            )
                        )
                    )

                    # Kick off a task to create and publish the next revocation
                    # registry in the background. It is assumed that the size of
                    # the registry is small enough so that this completes before
                    # we need it
                    revoc = IndyRevocation(self.context)
                    pending_registry_record = await revoc.init_issuer_registry(
                        active_reg.cred_def_id,
                        active_reg.issuer_did,
                        max_cred_num=active_reg.max_cred_num,
                    )
                    print(
                        Ink.GREEN(
                            ".. {} initialized pending rr {}, now in state {}".format(
                                time_now(),
                                pending_registry_record.revoc_reg_id,
                                pending_registry_record.state,
                            )
                        )
                    )
                    asyncio.ensure_future(
                        pending_registry_record.stage_pending_registry_definition(
                            self.context, max_attempts=16
                        )
                    )
                    print(
                        Ink.GREEN(
                            ".. {} ensured future stage-pending-def for rr {}".format(
                                time_now(), pending_registry_record.revoc_reg_id
                            )
                        )
                    )

                    # Check to see if we have a registry record staged and waiting
                    pending_rev_regs = await IssuerRevRegRecord.query_by_cred_def_id(
                        self.context,
                        cred_ex_record.credential_definition_id,
                        state=IssuerRevRegRecord.STATE_PUBLISHED,
                    )
                    print(
                        Ink.GREEN(
                            ".. {} found {} rev regs in state published: {}".format(
                                time_now(),
                                len(pending_rev_regs),
                                [prr.revoc_reg_id for prr in pending_rev_regs or []],
                            )
                        )
                    )
                    if pending_rev_regs:
                        pending_rev_reg = pending_rev_regs[0]
                        await pending_rev_reg.set_state(
                            self.context,
                            IssuerRevRegRecord.STATE_STAGED,
                        )
                        print(
                            Ink.GREEN(
                                ".. {} set rev reg {} state to staged".format(
                                    time_now(), pending_rev_reg.revoc_reg_id
                                )
                            )
                        )

                        # Make that one active
                        await pending_rev_reg.publish_registry_entry(self.context)
                        print(
                            Ink.GREEN(
                                ".. {} activated (sent entry) rev reg {} now {}".format(
                                    time_now(),
                                    pending_rev_reg.revoc_reg_id,
                                    pending_rev_reg.state,
                                )
                            )
                        )

                    # Make the current registry full
                    await active_reg.set_state(
                        self.context,
                        IssuerRevRegRecord.STATE_FULL,
                    )
                    print(
                        Ink.GREEN(
                            ".. {} set rev reg {} state full".format(
                                time_now(), active_reg.revoc_reg_id
                            )
                        )
                    )

            except IssuerRevocationRegistryFullError:
                print(
                    Ink.GREEN(
                        ".. {} rev reg {} full error with retries={}".format(
                            time_now(), cred_ex_record.revoc_reg_id, retries
                        )
                    )
                )
                active_rev_regs = await IssuerRevRegRecord.query_by_cred_def_id(
                    self.context,
                    cred_ex_record.credential_definition_id,
                    state=IssuerRevRegRecord.STATE_ACTIVE,
                )
                staged_rev_regs = await IssuerRevRegRecord.query_by_cred_def_id(
                    self.context,
                    cred_ex_record.credential_definition_id,
                    state=IssuerRevRegRecord.STATE_STAGED,
                )
                published_rev_regs = await IssuerRevRegRecord.query_by_cred_def_id(
                    self.context,
                    cred_ex_record.credential_definition_id,
                    state=IssuerRevRegRecord.STATE_PUBLISHED,
                )
                print(
                    Ink.GREEN(
                        ".. {} active rev regs {}, staged {}, published {}".format(
                            time_now(),
                            [rr.revoc_reg_id for rr in active_rev_regs or []],
                            [rr.revoc_reg_id for rr in staged_rev_regs or []],
                            [rr.revoc_reg_id for rr in published_rev_regs or []],
                        )
                    )
                )

                if (
                    staged_rev_regs or active_rev_regs or published_rev_regs
                ) and retries > 0:

                    # We know there is a staged registry that will be ready soon.
                    # So we wait and retry.
                    await asyncio.sleep(1)
                    print(
                        Ink.GREEN(
                            ".. {} Waited 1 sec and retrying issue-cred call".format(
                                time_now(),
                            )
                        )
                    )
                    return await self.issue_credential(
                        cred_ex_record=cred_ex_record,
                        comment=comment,
                        retries=retries - 1,
                    )
                else:
                    await active_reg.set_state(
                        self.context,
                        IssuerRevRegRecord.STATE_FULL,
                    )
                    print(
                        Ink.GREEN(
                            ".. {} No rev regs look promising: bailing here".format(
                                time_now(),
                            )
                        )
                    )
                    raise

            cred_ex_record.credential = json.loads(credential_json)

        cred_ex_record.state = V10CredentialExchange.STATE_ISSUED
        await cred_ex_record.save(self.context, reason="issue credential")

        credential_message = CredentialIssue(
            comment=comment,
            credentials_attach=[
                CredentialIssue.wrap_indy_credential(cred_ex_record.credential)
            ],
        )
        credential_message._thread = {"thid": cred_ex_record.thread_id}
        credential_message.assign_trace_decorator(
            self.context.settings, cred_ex_record.trace
        )

        return (cred_ex_record, credential_message)

    async def receive_credential(self) -> V10CredentialExchange:
        """
        Receive a credential from an issuer.

        Hold in storage potentially to be processed by controller before storing.

        Returns:
            Credential exchange record, retrieved and updated

        """
        credential_message = self.context.message
        assert len(credential_message.credentials_attach or []) == 1
        raw_credential = credential_message.indy_credential(0)

        (
            cred_ex_record
        ) = await V10CredentialExchange.retrieve_by_connection_and_thread(
            self.context,
            self.context.connection_record.connection_id,
            credential_message._thread_id,
        )

        cred_ex_record.raw_credential = raw_credential
        cred_ex_record.state = V10CredentialExchange.STATE_CREDENTIAL_RECEIVED

        await cred_ex_record.save(self.context, reason="receive credential")
        return cred_ex_record

    async def store_credential(
        self, cred_ex_record: V10CredentialExchange, credential_id: str = None
    ) -> Tuple[V10CredentialExchange, CredentialAck]:
        """
        Store a credential in holder wallet; send ack to issuer.

        Args:
            cred_ex_record: credential exchange record
                with credential to store and ack
            credential_id: optional credential identifier to override default on storage

        Returns:
            Tuple: (Updated credential exchange record, credential ack message)

        """
        if cred_ex_record.state != (V10CredentialExchange.STATE_CREDENTIAL_RECEIVED):
            raise CredentialManagerError(
                f"Credential exchange {cred_ex_record.credential_exchange_id} "
                f"in {cred_ex_record.state} state "
                f"(must be {V10CredentialExchange.STATE_CREDENTIAL_RECEIVED})"
            )

        raw_credential = cred_ex_record.raw_credential
        revoc_reg_def = None
        ledger: BaseLedger = await self.context.inject(BaseLedger)
        async with ledger:
            credential_definition = await ledger.get_credential_definition(
                raw_credential["cred_def_id"]
            )
            if (
                "rev_reg_id" in raw_credential
                and raw_credential["rev_reg_id"] is not None
            ):
                revoc_reg_def = await ledger.get_revoc_reg_def(
                    raw_credential["rev_reg_id"]
                )

        holder: BaseHolder = await self.context.inject(BaseHolder)
        if (
            cred_ex_record.credential_proposal_dict
            and "credential_proposal" in cred_ex_record.credential_proposal_dict
        ):
            mime_types = CredentialPreview.deserialize(
                cred_ex_record.credential_proposal_dict["credential_proposal"]
            ).mime_types()
        else:
            mime_types = None

        if revoc_reg_def:
            revoc_reg = RevocationRegistry.from_definition(revoc_reg_def, True)
            await revoc_reg.get_or_fetch_local_tails_path()
        try:
            credential_id = await holder.store_credential(
                credential_definition,
                raw_credential,
                cred_ex_record.credential_request_metadata,
                mime_types,
                credential_id=credential_id,
                rev_reg_def=revoc_reg_def,
            )
        except HolderError as e:
            self._logger.error(f"Error storing credential. {e.error_code}: {e.message}")
            raise e

        credential_json = await holder.get_credential(credential_id)
        credential = json.loads(credential_json)

        cred_ex_record.state = V10CredentialExchange.STATE_ACKED
        cred_ex_record.credential_id = credential_id
        cred_ex_record.credential = credential
        cred_ex_record.revoc_reg_id = credential.get("rev_reg_id", None)
        cred_ex_record.revocation_id = credential.get("cred_rev_id", None)

        await cred_ex_record.save(self.context, reason="store credential")

        credential_ack_message = CredentialAck()
        credential_ack_message.assign_thread_id(
            cred_ex_record.thread_id, cred_ex_record.parent_thread_id
        )
        credential_ack_message.assign_trace_decorator(
            self.context.settings, cred_ex_record.trace
        )

        if cred_ex_record.auto_remove:
            # Delete the exchange record since we're done with it
            await cred_ex_record.delete_record(self.context)

        return (cred_ex_record, credential_ack_message)

    async def receive_credential_ack(self) -> V10CredentialExchange:
        """
        Receive credential ack from holder.

        Returns:
            credential exchange record, retrieved and updated

        """
        credential_ack_message = self.context.message
        (
            cred_ex_record
        ) = await V10CredentialExchange.retrieve_by_connection_and_thread(
            self.context,
            self.context.connection_record.connection_id,
            credential_ack_message._thread_id,
        )

        cred_ex_record.state = V10CredentialExchange.STATE_ACKED
        await cred_ex_record.save(self.context, reason="credential acked")

        if cred_ex_record.auto_remove:
            # We're done with the exchange so delete
            await cred_ex_record.delete_record(self.context)

        return cred_ex_record

    async def revoke_credential(
        self, rev_reg_id: str, cred_rev_id: str, publish: bool = False
    ):
        """
        Revoke a previously-issued credential.

        Optionally, publish the corresponding revocation registry delta to the ledger.

        Args:
            rev_reg_id: revocation registry id
            cred_rev_id: credential revocation id
            publish: whether to publish the resulting revocation registry delta,
                along with any revocations pending against it

        """
        issuer: BaseIssuer = await self.context.inject(BaseIssuer)

        revoc = IndyRevocation(self.context)
        registry_record = await revoc.get_issuer_rev_reg_record(rev_reg_id)
        if not registry_record:
            raise CredentialManagerError(
                f"No revocation registry record found for id {rev_reg_id}"
            )

        if publish:
            rev_reg = await revoc.get_ledger_registry(rev_reg_id)
            await rev_reg.get_or_fetch_local_tails_path()

            # pick up pending revocations on input revocation registry
            crids = list(set(registry_record.pending_pub + [cred_rev_id]))
            (delta_json, _) = await issuer.revoke_credentials(
                registry_record.revoc_reg_id, registry_record.tails_local_path, crids
            )
            if delta_json:
                registry_record.revoc_reg_entry = json.loads(delta_json)
                await registry_record.publish_registry_entry(self.context)
                await registry_record.clear_pending(self.context)

        else:
            await registry_record.mark_pending(self.context, cred_rev_id)

    async def publish_pending_revocations(
        self, rrid2crid: Mapping[Text, Sequence[Text]] = None
    ) -> Mapping[Text, Sequence[Text]]:
        """
        Publish pending revocations to the ledger.

        Args:
            rrid2crid: Mapping from revocation registry identifiers to all credential
                revocation identifiers within each to publish. Specify null/empty map
                for all revocation registries. Specify empty sequence per revocation
                registry identifier for all pending within the revocation registry;
                e.g.,

            ::

                {} - publish all pending revocations from all revocation registries
                {
                    "R17v42T4pk...:4:R17v42T4pk...:3:CL:19:tag:CL_ACCUM:0": [],
                    "R17v42T4pk...:4:R17v42T4pk...:3:CL:19:tag:CL_ACCUM:1": ["1", "2"]
                } - publish:
                    - all pending revocations from all revocation registry tagged 0
                    - pending ["1", "2"] from revocation registry tagged 1
                    - no pending revocations from any other revocation registries.

        Returns: mapping from each revocation registry id to its cred rev ids published.
        """
        result = {}
        issuer: BaseIssuer = await self.context.inject(BaseIssuer)

        registry_records = await IssuerRevRegRecord.query_by_pending(self.context)
        for registry_record in registry_records:
            rrid = registry_record.revoc_reg_id
            crids = []
            if not rrid2crid:
                crids = registry_record.pending_pub
            elif rrid in rrid2crid:
                crids = [
                    crid
                    for crid in registry_record.pending_pub
                    if crid in (rrid2crid[rrid] or []) or not rrid2crid[rrid]
                ]
            if crids:
                (delta_json, failed_crids) = await issuer.revoke_credentials(
                    registry_record.revoc_reg_id,
                    registry_record.tails_local_path,
                    crids,
                )
                registry_record.revoc_reg_entry = json.loads(delta_json)
                await registry_record.publish_registry_entry(self.context)
                published = [crid for crid in crids if crid not in failed_crids]
                result[registry_record.revoc_reg_id] = published
                await registry_record.clear_pending(self.context, published)

        return result

    async def clear_pending_revocations(
        self, purge: Mapping[Text, Sequence[Text]] = None
    ) -> Mapping[Text, Sequence[Text]]:
        """
        Clear pending revocation publications.

        Args:
            purge: Mapping from revocation registry identifiers to all credential
                revocation identifiers within each to clear. Specify null/empty map
                for all revocation registries. Specify empty sequence per revocation
                registry identifier for all pending within the revocation registry;
                e.g.,

            ::

                {} - clear all pending revocations from all revocation registries
                {
                    "R17v42T4pk...:4:R17v42T4pk...:3:CL:19:tag:CL_ACCUM:0": [],
                    "R17v42T4pk...:4:R17v42T4pk...:3:CL:19:tag:CL_ACCUM:1": ["1", "2"]
                } - clear:
                    - all pending revocations from all revocation registry tagged 0
                    - pending ["1", "2"] from revocation registry tagged 1
                    - no pending revocations from any other revocation registries.

        Returns:
            mapping from revocation registry id to its remaining
            cred rev ids still marked pending, omitting revocation registries
            with no remaining pending publications.

        """
        result = {}
        registry_records = await IssuerRevRegRecord.query_by_pending(self.context)
        for registry_record in registry_records:
            rrid = registry_record.revoc_reg_id
            await registry_record.clear_pending(self.context, (purge or {}).get(rrid))
            if registry_record.pending_pub:
                result[rrid] = registry_record.pending_pub

        return result
