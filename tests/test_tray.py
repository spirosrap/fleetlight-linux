import unittest

from fleetlight.tray import ICON, UPDATING_ICON, icon_name, item_properties, layout


class TrayMenu(unittest.TestCase):
    def test_layout_numbers_entries_and_marks_information_rows_disabled(self):
        root, properties, children = layout([("3 of 4 online", None), None, ("Quit", lambda: None)])
        self.assertEqual(root, 0)
        self.assertEqual(properties["children-display"].unpack(), "submenu")
        rows = [child.unpack() for child in children]
        self.assertEqual([row[0] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0][1], {"label": "3 of 4 online", "enabled": False})
        self.assertEqual(rows[1][1], {"type": "separator"})
        self.assertEqual(rows[2][1], {"label": "Quit", "enabled": True})

    def test_underscores_are_not_read_as_mnemonics(self):
        self.assertEqual(item_properties(("build_host", None))["label"].unpack(), "build__host")

    def test_icon_switches_while_updates_are_installed(self):
        self.assertEqual(icon_name(False), ICON)
        self.assertEqual(icon_name(True), UPDATING_ICON)
        self.assertNotEqual(ICON, UPDATING_ICON)
        for name in (ICON, UPDATING_ICON):
            self.assertFalse(name.endswith("-symbolic"), "bars recolour symbolic icons; Omarchy blanks an item that switches back from a coloured one")


if __name__ == "__main__":
    unittest.main()
