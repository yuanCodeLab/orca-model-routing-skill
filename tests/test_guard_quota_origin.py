"""An account directory can contain hardlinked shared rollouts resumed with another login."""
import os
from pathlib import Path
from unittest.mock import patch
from test_codex_session_log import Base, event, orca_output, WEEKLY_RESET


class HardlinkIdentity(Base):
    def linked(self, session, weekly, shift=1711):
        active=self.root/'active'; shared=self.root/'shared'
        active.mkdir(exist_ok=True); shared.mkdir(exist_ok=True)
        path=active/'rollout-resumed.jsonl'
        path.unlink(missing_ok=True)
        target=shared/'rollout-resumed.jsonl';target.unlink(missing_ok=True)
        target.write_text(event(age=5, session=session, weekly=weekly, weekly_reset=WEEKLY_RESET-shift))
        os.link(target,path)
        return active

    def test_other_login_high_remaining_cannot_hide_real_low_quota(self):
        active=self.linked(5,17)
        with patch('quota.codex_log_roots',return_value=[active]):
            c=self.codex(orca_output(age=10,session=90,weekly=62),roots=[active])
        self.assertEqual((c['data_source'],c['session']['usedPercent']),('orca',90))
        self.assertEqual(c['log_status'],'account_mismatch')

    def test_other_login_low_remaining_cannot_false_trigger(self):
        active=self.linked(99,99)
        with patch('quota.codex_log_roots',return_value=[active]):
            c=self.codex(orca_output(age=10,session=10,weekly=10),roots=[active])
        self.assertEqual((c['data_source'],c['session']['usedPercent']),('orca',10))

    def test_matching_weekly_shared_inode_is_still_accepted(self):
        active=self.linked(90,62,shift=1)
        with patch('quota.codex_log_roots',return_value=[active]):
            c=self.codex(roots=[active])
        self.assertEqual((c['data_source'],c['session']['usedPercent']),('codex_session_log',90))

    def test_unknown_reference_and_shared_inode_fails_closed(self):
        active=self.linked(5,17)
        import json
        data=json.loads(orca_output());data['result']['rateLimits']['codex'].update(status='error',session=None,weekly=None)
        with patch('quota.codex_log_roots',return_value=[active]):
            c=self.codex(json.dumps(data),roots=[active])
        self.assertEqual(c['data_source'],'orca')
        self.assertEqual(c['status'],'error')
