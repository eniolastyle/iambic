import asyncio
from itertools import chain
from typing import List, Optional

from pydantic import Field

from iambic.aws.models import ExpiryModel
from iambic.config.models import GoogleProject
from iambic.core.context import ExecutionContext
from iambic.core.logger import log
from iambic.core.models import AccountChangeDetails, ProposedChange, ProposedChangeType
from iambic.google.group.utils import (
    create_group,
    get_group,
    update_group_description,
    update_group_domain,
    update_group_email,
    update_group_members,
    update_group_name,
)
from iambic.google.models import (
    GoogleTemplate,
    GroupMemberRole,
    GroupMemberStatus,
    GroupMemberSubscription,
    GroupMemberType,
    WhoCanInvite,
    WhoCanJoin,
    WhoCanPostMessage,
    WhoCanViewGroup,
    WhoCanViewMembership,
)


class GroupMember(ExpiryModel):
    email: str
    expand: bool = Field(
        False,
        description="Expand the group into the members of the group. This is useful for nested groups.",
    )
    role: GroupMemberRole = GroupMemberRole.MEMBER
    type: GroupMemberType = GroupMemberType.USER
    status: GroupMemberStatus = GroupMemberStatus.ACTIVE
    subscription: GroupMemberSubscription = GroupMemberSubscription.EACH_EMAIL

    @property
    def resource_type(self):
        return "google:group:member"

    @property
    def resource_id(self):
        return self.email


class GroupTemplate(GoogleTemplate):
    template_type = "NOQ::Google::Group"
    name: str
    domain: str
    email: str
    description: str
    welcome_message: Optional[str]
    members: List[GroupMember]
    who_can_invite: WhoCanInvite = "ALL_MANAGERS_CAN_INVITE"
    who_can_join: WhoCanJoin = "CAN_REQUEST_TO_JOIN"
    who_can_post_message: WhoCanPostMessage = "NONE_CAN_POST"
    who_can_view_group: WhoCanViewGroup = "ALL_MANAGERS_CAN_VIEW"
    who_can_view_membership: WhoCanViewMembership = "ALL_MANAGERS_CAN_VIEW"
    read_only: bool = False
    # TODO: who_can_contact_group_members
    # TODO: who_can_view_member_email_addresses
    # TODO: allow_email_posting
    # TODO: allow_web_posting
    # TODO: conversation_history
    # TODO: There is more. Check google group settings page

    def apply_resource_dict(
        self, google_project: GoogleProject, context: ExecutionContext
    ):
        return {
            "name": self.name,
            "email": self.email,
            "description": self.description,
            "members": self.members,
        }

    async def _apply_to_account(
        self, google_project: GoogleProject, context: ExecutionContext
    ) -> AccountChangeDetails:
        proposed_group = self.apply_resource_dict(google_project, context)
        change_details = AccountChangeDetails(
            account=self.domain,
            resource_id=self.email,
            new_value=proposed_group,  # TODO fix
            proposed_changes=[],
        )

        log_params = dict(
            resource_type=self.resource_type,
            resource_id=self.email,
            account=str(self.domain),
        )
        # read_only = self._is_read_only(google_project)

        current_group = await get_group(self.email, self.domain, google_project)
        if current_group:
            change_details.current_value = current_group
        # TODO: Check if deleted
        # deleted = self.get_attribute_val_for_account(aws_account, "deleted", False)
        # if isinstance(deleted, list):
        #     deleted = deleted[0].deleted
        # if deleted:
        #     if current_group:
        #         # Delete me

        group_exists = bool(current_group)

        tasks = []

        if not group_exists:
            change_details.proposed_changes.append(
                ProposedChange(
                    change_type=ProposedChangeType.CREATE,
                    resource_id=self.email,
                    resource_type=self.resource_type,
                )
            )
            log_str = "New resource found in code."
            if not context.execute:
                log.info(log_str, **log_params)
                # Exit now because apply functions won't work if resource doesn't exist
                return change_details

            log_str = f"{log_str} Creating resource..."
            log.info(log_str, **log_params)

            await create_group(
                id=self.email,
                domain=self.domain,
                email=self.email,
                name=self.name,
                description=self.description,
                google_project=google_project,
            )
            current_group = await get_group(self.email, self.domain, google_project)
            if current_group:
                change_details.current_value = current_group

        # TODO: Support group expansion
        tasks.extend(
            [
                update_group_domain(
                    current_group.domain, self.domain, log_params, context
                ),
                update_group_email(
                    current_group.email,
                    self.email,
                    self.domain,
                    google_project,
                    log_params,
                    context,
                ),
                update_group_name(
                    self.email,
                    current_group.name,
                    self.name,
                    self.domain,
                    google_project,
                    log_params,
                    context,
                ),
                update_group_description(
                    self.email,
                    current_group.description,
                    self.description,
                    self.domain,
                    google_project,
                    log_params,
                    context,
                ),
                update_group_members(
                    self.email,
                    current_group.members,
                    self.members,
                    self.domain,
                    google_project,
                    log_params,
                    context,
                ),
            ]
        )

        changes_made = await asyncio.gather(*tasks)
        if any(changes_made):
            change_details.proposed_changes.extend(
                list(chain.from_iterable(changes_made))
            )

        if context.execute:
            log.debug(
                "Successfully finished execution for resource",
                changes_made=bool(change_details.proposed_changes),
                **log_params,
            )
        else:
            log.debug(
                "Successfully finished scanning for drift for resource",
                requires_changes=bool(change_details.proposed_changes),
                **log_params,
            )

        return change_details

    @property
    def resource_type(self):
        return "google:group"

    def _is_read_only(self, google_project: GoogleProject):
        return google_project.read_only or self.read_only


async def get_group_template(service, group, domain) -> GroupTemplate:
    member_req = service.members().list(groupKey=group["email"])
    member_res = member_req.execute() or {}
    members = [
        GroupMember(
            email=member["email"],
            role=GroupMemberRole(member["role"]),
            type=GroupMemberType(member["type"]),
            status=GroupMemberStatus(member["status"]),
        )
        for member in member_res.get("members", [])
    ]
    file_name = f"{group['email'].split('@')[0]}.yaml"
    return GroupTemplate(
        file_path=f"google_groups/{domain}/{file_name}",
        domain=domain,
        name=group["name"],
        email=group["email"],
        description=group["description"],
        members=members,
    )