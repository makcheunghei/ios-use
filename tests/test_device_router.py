"""Device selection must never redirect a pointer, continuation, preview or setup job."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import Runtime
from wda_client import WDAError

PAD = "00000000-0000000000000001"
PHONE = "00000000-0000000000000002"


class DeviceRouterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"IPHONE_USE_ANALYTICS": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.root = Runtime(directory.name)
        self.addCleanup(self.root.close)
        self.router = self.root.router
        self.devices = [{"model":"iPad Pro","udid": PAD}, {"model":"iPhone","udid":PHONE}]
        discover = patch.object(self.root.setup_manager, "discover", return_value={"ok":True,"devices":self.devices})
        discover.start()
        self.addCleanup(discover.stop)
        for device, port in [(PAD,18220),(PHONE,18221)]:
            where = self.router.directory / device
            where.mkdir(parents=True)
            (where / "config.json").write_text(json.dumps({"udid":device,"local_port":port}))
        (self.root.state_dir / "config.json").write_text(json.dumps({"udid":PHONE,"local_port":18100}))

    def test_alias_default_and_exact_id_select_the_same_private_runtime(self):
        for target, device in [("ipad", PAD),("iphone",PHONE),(PAD,PAD),(None,PHONE)]:
            args = {"device":target} if target else {}
            res = self.root.call("pua_screen", args)
            self.assertEqual(res["device_id"],device)
        pad, phone = (self.router.sessions[self.router.directory / d] for d in (PAD,PHONE))
        self.assertNotEqual(pad.base_url,phone.base_url)
        self.assertIsNot(pad.client,phone.client)
        self.assertIsNot(pad.screen,phone.screen)
        with patch.object(pad.phone,"rotation_side",return_value="left"):
            observation = pad.phone.remember([],{"width":1376,"height":1032},"test")
        with self.assertRaises(WDAError) as err:
            phone.phone.guard(observation)
        self.assertEqual(err.exception.code,"stale_observation")
        with self.assertRaises(WDAError):
            phone.phone.continue_input("another-device-token")
        self.assertFalse(phone.client.records)

    def test_ambiguity_lists_candidates_and_invalid_batch_never_reroutes(self):
        other = "00000000-0000000000000003"
        self.devices.append({"udid":other,"model":"iPad mini"})
        with self.assertRaises(WDAError) as err:
            self.root.call("pua_screen",{"device":"ipad"})
        self.assertEqual(err.exception.details["candidates"],[PAD,other])
        with self.assertRaises(WDAError) as err:
            self.root.call("pua_batch",{"device":PHONE,"steps":[{"op":"tap","args":{"device":PAD,"x":1,"y":2}}]})
        self.assertEqual(err.exception.code,"invalid_argument")
        self.assertFalse(self.router.sessions[self.router.directory / PHONE].client.records)

    def test_port_conflict_precedes_configuration_and_preserves_other_profile(self):
        with self.assertRaises(WDAError) as caught:
            self.root.call("pua_setup",{"action":"configure","device":"ipad","local_port":18221})
        self.assertEqual(caught.exception.code,"port_conflict")
        self.assertEqual(json.loads((self.router.directory / PAD / "config.json").read_text())["local_port"],18220)
