"""External Contact tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import DESTRUCTIVE, READ_ONLY
from ..client import get_client

# Copied verbatim from the old contact into the new one. Deliberately excludes
# id, division, createDate, modifyDate, type, selfUri (system-generated),
# canonicalContact, mergeSet, mergedFrom, mergedTo, mergeOperation and
# identifiers - the merge history is exactly what the reset is clearing.
CORE_FIELDS = [
    "firstName", "middleName", "lastName", "salutation", "title",
    "workPhone", "cellPhone", "homePhone", "otherPhone",
    "workEmail", "personalEmail", "otherEmail",
    "address", "twitterId", "lineId", "whatsAppId", "facebookId",
    "instagramId", "appleOpaqueIds", "externalIds",
    "externalOrganization", "surveyOptOut", "externalSystemUrl",
    "customFields",
]

PHONE_SUBFIELDS = ["display", "extension", "acceptsSMS"]
PHONE_FIELDS = {"workPhone", "cellPhone", "homePhone", "otherPhone"}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def find_external_contact(query: str) -> list[dict[str, Any]]:
        """Search External Contacts by email, phone or name.

        Also reports how many identities are stitched onto each contact, which
        is what determines whether it needs a reset.

        The search is fuzzy, so a full address still matches its neighbours:
        looking for zz.verification@example.invalid also returns
        zz.verification.twin@example.invalid. Demo contacts sharing an address
        prefix will therefore collide.

        It is also indexed separately from the contacts themselves. A contact
        created seconds ago is not findable yet even though fetching it by id
        works, and just after a reset the search can still return the deleted
        contact. An unexpected result here is usually the index catching up.

        For a contact with merge history the id returned is the canonical one
        for the merged cluster, which is not necessarily the id last written to.
        Both resolve to the same data, so an id that differs from the expected
        one is not a sign that the wrong contact was found.
        """
        client = get_client()
        # Without the expand, externalOrganization comes back as a bare id
        # reference and the organization reads as null on a contact that has one.
        found = client.get(
            "/api/v2/externalcontacts/contacts",
            params={"q": query, "expand": "externalOrganization"},
        )
        results = []
        for contact in found.get("entities", []):
            results.append(
                {
                    "id": contact.get("id"),
                    "name": " ".join(filter(None, [contact.get("firstName"), contact.get("lastName")])),
                    "work_email": contact.get("workEmail"),
                    "cell_phone": (contact.get("cellPhone") or {}).get("display"),
                    "organization": (contact.get("externalOrganization") or {}).get("name"),
                    "stitched_identity_count": len(contact.get("mergeSet") or []),
                }
            )
        return results

    @mcp.tool(annotations=DESTRUCTIVE)
    def reset_external_contact(query: str) -> dict[str, Any]:
        """Reset an External Contact that has hit the identity-stitching limit.

        Genesys Cloud adds a mergeSet entry every time a new visitor identity
        (usually a browser cookie) is stitched onto a contact through a matching
        email or phone number. Demo runs against the same fixed test identities
        inflate that list quickly, and once the platform limit is reached,
        identity stitching starts failing outright.

        This reads the contact's core fields, deletes it, and recreates it with
        the same values. Merge history goes back to zero while the contact keeps
        working for anything that looks it up by email or phone. It does get a
        new id, so this is only safe when nothing references the contact id
        directly.

        Because the contact search is fuzzy, refusing an ambiguous query is not
        a formality: a full email address can match a second contact whose
        address merely shares a prefix. Narrow the query rather than forcing it.

        Anything that looks the contact up straight afterwards should allow a
        few seconds. The search index trails the reset and answers with the
        deleted contact for a moment, which looks like a failed reset.

        Args:
            query: Email, phone or name identifying the contact. If more than
                one matches, nothing is deleted and the candidates are returned
                so the caller can be more specific.
        """
        client = get_client()
        found = client.get("/api/v2/externalcontacts/contacts", params={"q": query})
        entities = found.get("entities", [])

        if not entities:
            raise ToolError(f"No external contact matches {query!r}.")
        if len(entities) > 1:
            candidates = [
                {
                    "id": e.get("id"),
                    "name": " ".join(filter(None, [e.get("firstName"), e.get("lastName")])),
                    "work_email": e.get("workEmail"),
                }
                for e in entities
            ]
            raise ToolError(
                f"{len(entities)} contacts match {query!r}. Nothing was deleted. "
                f"Narrow the query, candidates: {candidates}"
            )

        old = entities[0]
        old_id = old["id"]
        identifiers = client.get(f"/api/v2/externalcontacts/contacts/{old_id}/identifiers")
        identifier_entities = identifiers.get("entities", [])

        new_body: dict[str, Any] = {}
        for field in CORE_FIELDS:
            value = old.get(field)
            if value is None:
                continue
            if field in PHONE_FIELDS:
                # The platform re-derives e164, countryCode and the rest from
                # "display" on create, so only the authored subfields are kept.
                value = {k: v for k, v in value.items() if k in PHONE_SUBFIELDS and v is not None}
            elif field == "externalOrganization":
                value = {"id": value["id"]}
            new_body[field] = value

        if old.get("schema"):
            new_body["schema"] = {"id": old["schema"]["id"], "version": old["schema"]["version"]}

        client.request("DELETE", f"/api/v2/externalcontacts/contacts/{old_id}")
        new = client.request("POST", "/api/v2/externalcontacts/contacts", json_body=new_body)

        return {
            "old_id": old_id,
            "new_id": new["id"],
            "name": " ".join(filter(None, [new.get("firstName"), new.get("lastName")])),
            "cleared_stitched_identities": len(old.get("mergeSet") or []),
            "cleared_identifiers": len(identifier_entities),
        }
