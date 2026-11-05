"""Inputs are resolved by their role, not by global recursive match order."""
from pathlib import Path
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from abmt_paths import find_input_file


class InputPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, relative, text="test"):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p.resolve()

    def test_boundary_direct_path_wins_over_duplicate_backup(self):
        expected = self.put("Boundary/neuchatel.shp")
        self.put("old_extraction/Boundary/neuchatel.shp")
        self.assertEqual(find_input_file(self.root, "neuchatel.shp"), expected)

    def test_direct_boundary_wins_over_nested_copy(self):
        expected = self.put("Boundary/neuchatel.shp")
        self.put("Boundary/old/neuchatel.shp")
        self.assertEqual(find_input_file(self.root, "neuchatel.shp"), expected)

    def test_survey_is_not_read_from_backup(self):
        expected = self.put("Mikrozensus/wege.csv")
        self.put("backup/microcensus/wege.csv")
        self.assertEqual(find_input_file(self.root, "wege.csv"), expected)

    def test_lowercase_survey_folder_accepts_old_folder_hint(self):
        expected = self.put("microcensus/haushalte.csv")
        self.assertEqual(find_input_file(self.root, "haushalte.csv", "Mikrozensus"), expected)

    def test_population_and_simulation_have_distinct_scopes(self):
        population = self.put("Neuchatel_10pct/neuchatel_population.xml.gz")
        self.put("backup/neuchatel_population.xml.gz")
        simulation = self.put("Neuchatel_10pct/simulation_output/output_trips.csv.gz")
        self.put("older_run/simulation_output/output_trips.csv.gz")
        self.assertEqual(find_input_file(self.root, "neuchatel_population.xml.gz"), population)
        self.assertEqual(find_input_file(self.root, "output_trips.csv.gz"), simulation)

    def test_two_survey_datasets_remain_explicitly_ambiguous(self):
        self.put("microcensus/wege.csv", "a")
        self.put("Mikrozensus/wege.csv", "b")
        # Case-insensitive filesystems resolve these to the same input.
        paths = {(self.root/p).resolve() for p in ["microcensus/wege.csv", "Mikrozensus/wege.csv"]}
        if len(paths) == 1 or (self.root/'microcensus/wege.csv').samefile(self.root/'Mikrozensus/wege.csv'):
            self.skipTest("Survey aliases identify one file on this filesystem.")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            find_input_file(self.root, "wege.csv")

    def test_nested_survey_duplicates_remain_ambiguous(self):
        self.put("microcensus/first/wege.csv")
        self.put("microcensus/second/wege.csv")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            find_input_file(self.root, "wege.csv")

    def test_missing_canonical_input_does_not_select_backup(self):
        (self.root/'Boundary').mkdir()
        self.put("backup/Boundary/neuchatel.shp")
        with self.assertRaises(FileNotFoundError):
            find_input_file(self.root, "neuchatel.shp")

    def test_unambiguous_legacy_layout_is_supported(self):
        expected = self.put("legacy/survey/wege.csv")
        self.assertEqual(find_input_file(self.root, "wege.csv"), expected)

    def test_filename_case_is_supported(self):
        expected = self.put("Boundary/Neuchatel.shp")
        self.assertEqual(find_input_file(self.root, "neuchatel.shp"), expected)

    def test_aliases_of_the_same_file_are_not_a_second_dataset(self):
        original = self.put("microcensus/wege.csv")
        alias = self.root/'Mikrozensus/wege.csv'
        alias.parent.mkdir(exist_ok=True)
        if not alias.exists():
            os.link(original, alias)
        selected = find_input_file(self.root, 'wege.csv')
        self.assertTrue(selected.samefile(original))

    def test_macos_metadata_is_not_a_second_input(self):
        expected = self.put("legacy/neuchatel.shp")
        self.put("__MACOSX/legacy/neuchatel.shp")
        self.assertEqual(find_input_file(self.root, "neuchatel.shp"), expected)


if __name__ == "__main__":
    unittest.main()
