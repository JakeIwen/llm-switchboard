"""The Mac link's operator ops on the hub (a `Hub` mixin): `link_drop`, the
signer check `desk link-authorize` and `desk link-revoke` confirm with,
and `link_fence`, the rollback fence (docs/voice-desk.md, "SSH",
"Switching it off, revocation and rollback"). Neither is offered on the
page and no HTTP route reaches them.

`link_drop {host}` runs the signer check now: it rereads `link-signers`,
retires every link the file no longer lists (dropping its registrations)
and answers, from that one hold of the links lock, `{signer, link}`: the
host's signer line as it read it and the host's live link, each null or
`{fingerprint}`, plus `readable` (False when the file couldn't be read,
which retires every link). It never waits on a link.

`link_fence` takes the links lock once (`LinkHub.fence_check`: no signer
line, no live link, no handoff past the gate), releases it, then takes
the state's lock once (`HubState.fence_lock_view`: the list, the store's
error and `locks.json` from disk) and refuses on any Mac lock in either.
It changes nothing and holds nothing after it answers. The mixin uses
Hub's `config`, `links` and `state`.
"""

import logging

from .link_fence import refuse_lock_view
from .locks import inspect_lock_file
from .remote_enrollment import check_mac_host

log = logging.getLogger("llm_switchboard.desk")


class HubLinkOps:
    def _op_link_drop(self, message: dict) -> dict:
        host = check_mac_host(message.get("host"))
        answer = self.links.check_signers(host)
        log.info("desk: link_drop for %s: signer %s, link %s", host, "listed" if answer["signer"] else "none",
                 "live" if answer["link"] else "none")
        return answer

    def _op_link_fence(self, message: dict) -> dict:
        self.links.fence_check()
        refuse_lock_view(self.state.fence_lock_view(lambda: inspect_lock_file(self.config.lock_file)))
        log.info("desk: link-fence passed")
        return {"fenced": True}
