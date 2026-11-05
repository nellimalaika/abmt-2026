"""Artificial-data regressions for the two integrated PT analyses and fixes."""
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_fixture import make_fixture
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import box

import _abmt_accessibility as access
import _abmt_subscriptions as subs
import _abmt_age as age
import _abmt_modes as modes
import _abmt_spatial as spatial
import _abmt_trips as trips
import abmt_common as common


class SubscriptionRules(unittest.TestCase):
    def row(self, **changes):
        return dict(age=30, f41610a=2, f41610b=2, f41610c=2,
                    f41610d=2, f41610f=2) | changes

    def test_all_six_categories_and_ga_priority(self):
        cases=[({}, 'none'), ({'f41610a':1,'f41610b':None}, 'ga'),
               ({'f41610c':1}, 'va'), ({'f41610b':1}, 'ht'),
               ({'f41610b':1,'f41610c':1}, 'va+ht'), ({'f41610a':-97}, 'unknown')]
        for updates, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(subs.classify_survey(self.row(**updates), 'generator'), expected)

    def test_junior_enkel_only_below_16(self):
        for years in [6,15,16,17,120]:
            row=self.row(age=years, f41610f=1)
            self.assertEqual(subs.classify_survey(row,'generator'),'ga' if years<16 else 'none')
            self.assertEqual(subs.classify_survey(row,'three_items'),'none')

    def test_route_pass_and_literal_sensitivity(self):
        row=self.row(f41610d=1,f41610b=1)
        self.assertEqual(subs.classify_survey(row,'generator'),'va+ht')
        self.assertEqual(subs.classify_survey(row,'three_items'),'ht')

    def test_missing_items_and_structural_inapplicability(self):
        for missing in [None, np.nan, -97, -98, -99, 0, 3, 'bad']:
            with self.subTest(missing=missing):
                self.assertEqual(subs.classify_survey(self.row(f41610b=missing),'generator'),'unknown')
        self.assertEqual(subs.classify_survey(self.row(age=15,f41610b=-99),'generator'),'none')
        self.assertEqual(subs.classify_survey(self.row(age=16,f41610b=-99),'generator'),'unknown')
        self.assertEqual(subs.classify_survey(self.row(age=15,f41610f=-99),'generator'),'unknown')

    def test_three_valued_or_and_confirmed_ga_resolves_unknowns(self):
        self.assertEqual(subs.classify_survey(self.row(f41610c=-97,f41610d=1),'generator'),'va')
        self.assertEqual(subs.classify_survey(self.row(age=15,f41610a=-97,f41610f=1,
            f41610b=None,f41610c=None,f41610d=None),'generator'),'ga')
        self.assertEqual(subs.classify_survey(self.row(f41610d=None),'generator'),'unknown')

    def test_weights_unknowns_and_nontravellers_stay_in_denominator(self):
        survey=pd.DataFrame([self.row()|{'weight':2.,'f50200':2},
                             self.row(f41610a=-97)|{'weight':6.,'f50200':1}])
        sim=pd.DataFrame({'weight':[1.,1.], 'subscriptions':['none',None]})
        for definition in subs.DEFINITIONS:
            table,_=subs.compare(survey,sim,definition)
            self.assertEqual(table.survey_n.sum(),2)
            self.assertEqual(table.synthetic_n.sum(),2)
            self.assertEqual(table.survey_weight_sum.sum(),8.)
            self.assertAlmostEqual(table.survey_weighted_pct.sum(),100)
            self.assertAlmostEqual(table.synthetic_pct.sum(),100)
            self.assertEqual(table.loc[table.category.eq('unknown'),'survey_weighted_pct'].item(),75.)
            self.assertEqual(table.loc[table.category.eq('none'),'synthetic_pct'].item(),50.)


class CommonLayerRules(unittest.TestCase):
    def residents(self, prefix, n, klass='D', x=2_600_100., weight=1.):
        identifier='HHNR' if prefix=='survey' else 'person'
        return pd.DataFrame({identifier:[f'{prefix}{i}' for i in range(n)],
            'x':[x]*n,'y':[1_200_100.]*n,'weight':[weight]*n,'pt_class':[klass]*n})

    def test_boundary_overlap_best_grade_and_no_hit(self):
        frame=pd.DataFrame({'person':['a','b','c','d'], 'x':[0.,2.,5.,9.], 'y':[1.]*4,
                            'pt_class':['unknown','D','no_class','A'],'weight':[2.,3.,5.,7.]})
        polys=[box(0,0,4,2),box(2,0,6,2)]
        result,audit=access.assign_common_classes(frame,polys,['D','A'])
        self.assertEqual(result.pt_class.tolist(),['D','A','A','no_class'])
        self.assertEqual(result.supplied_class.tolist(),frame.pt_class.tolist())
        self.assertEqual(audit['homes_covered_by_multiple_class_labels'],1)
        pd.testing.assert_series_equal(result.weight,frame.weight)
        pd.testing.assert_series_equal(result.person,frame.person)
        transitions=access.transitions(result)
        self.assertEqual(transitions.persons.sum(),4)
        self.assertEqual(transitions.weight_sum.sum(),17.)
        self.assertAlmostEqual(transitions.weighted_pct.sum(),100.)

    def test_same_assignment_rule_for_both_sources(self):
        a=self.residents('survey',3); b=self.residents('synthetic',3)
        polygon=box(2_600_100,1_200_000,2_600_300,1_200_300)
        left,_=access.assign_common_classes(a,[polygon],['B'])
        right,_=access.assign_common_classes(b,[polygon],['B'])
        self.assertEqual(left.pt_class.tolist(),right.pt_class.tolist())

    def test_unknown_supplied_attribute_is_not_no_class(self):
        for value in [None,np.nan,'','null','NaN','1',1,'n/a']:
            self.assertEqual(access.model_class(value),'unknown')
        self.assertEqual(access.model_class('None'),'no_class')
        self.assertEqual(access.model_class(' d '),'D')
        self.assertEqual(access.survey_class(5),'no_class')
        self.assertEqual(access.survey_class(-99),'unknown')

    def test_reference_crs_header_validation(self):
        polygon=box(0,0,1,1)
        blob=b'GP'+bytes([0,1])+struct.pack('<i',2056)+polygon.wkb
        self.assertTrue(access.decode_gpkg_geometry(blob).equals(polygon))
        with self.assertRaisesRegex(ValueError,'2056'):
            access.decode_gpkg_geometry(blob[:4]+struct.pack('<i',21781)+blob[8:])

    def test_checksum_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(access.urllib.request,'urlopen',return_value=io.BytesIO(b'not the archive')):
                with self.assertRaisesRegex(ValueError,'checksum'):
                    access.ensure_reference(Path(temp))
            self.assertFalse(list(Path(temp).glob('*.part')))

    def test_cached_geopackage_is_verified_without_a_download(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'OeV_Gueteklassen_ARE_2015.gpkg';path.write_bytes(b'fixture')
            with patch.object(access,'GPKG_SHA256',access.sha256(path)), patch.object(access.urllib.request,'urlopen') as network:
                self.assertEqual(access.ensure_reference(Path(temp)),path)
                network.assert_not_called()

    def test_extracted_geopackage_checksum_is_also_required(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'gueteklassen_oev_2015_2056.gpkg.zip'
            with zipfile.ZipFile(archive,'w') as target:
                target.writestr(access.GPKG_MEMBER,b'corrupt geopackage')
            with patch.object(access,'ZIP_SHA256',access.sha256(archive)):
                with self.assertRaisesRegex(ValueError,'GeoPackage checksum'):
                    access.ensure_reference(Path(temp))
            self.assertFalse((Path(temp)/'OeV_Gueteklassen_ARE_2015.gpkg').exists())

    def test_global_grid_alignment_and_outer_boundary_assignment(self):
        polygon=box(2_600_000,1_200_000,2_620_000,1_210_000)
        grid,geometry=spatial.make_grid(polygon,common.GRID_M)
        self.assertTrue(grid.grid_xmin_m.mod(10_000).eq(0).all())
        self.assertTrue(grid.grid_ymin_m.mod(10_000).eq(0).all())
        frame=pd.DataFrame({'x':[2_600_000,2_610_000,2_620_000], 'y':[1_200_000]*3})
        self.assertEqual(spatial.assign_cells(frame,geometry,10_000),[(260,120),(261,120),(261,120)])

    def test_threshold_29_vs_30_requires_both_sources(self):
        polygon=box(2_600_000,1_200_000,2_620_000,1_210_000)
        grid,geometry=spatial.make_grid(polygon,10_000)
        for n,m,expected in [(29,30,False),(30,29,False),(30,30,True)]:
            cells=access.aggregate_quality_grid(self.residents('survey',n,weight=1000.),
                self.residents('synthetic',m),grid,geometry,10_000,30,30)
            row=cells.loc[cells.survey_n.gt(0)].iloc[0]
            self.assertEqual(bool(row.difference_shown),expected)
            self.assertEqual(pd.notna(row.difference_pp_displayed),expected)

    def test_duplicate_records_cannot_inflate_distinct_support(self):
        a=self.residents('survey',30);a.HHNR='same resident'
        grid,geometry=spatial.make_grid(box(2_600_000,1_200_000,2_610_000,1_210_000),10_000)
        with self.assertRaisesRegex(ValueError,'distinct resident'):
            access.aggregate_quality_grid(a,self.residents('synthetic',30),grid,geometry,10_000,30,30)

    def test_within_cell_classified_denominator_and_coverage(self):
        a=self.residents('survey',32,klass='A',weight=2.)
        a.loc[:9,'pt_class']='D';a.loc[10:19,'pt_class']='no_class'
        a.loc[30:,'pt_class']='unknown';a.loc[30:,'weight']=100.
        b=self.residents('synthetic',30,klass='A')
        grid,geometry=spatial.make_grid(box(2_600_000,1_200_000,2_620_000,1_210_000),10_000)
        cells=access.aggregate_quality_grid(a,b,grid,geometry,10_000,30,30)
        row=cells.loc[cells.survey_n.gt(0)].iloc[0]
        self.assertAlmostEqual(row.survey_low_service_pct,100*40/60)
        self.assertAlmostEqual(row.survey_unknown_pct,100*200/260)
        self.assertEqual(row.survey_known_n,30)
        self.assertTrue(row.difference_shown)
        coverage=access.coverage_summary(cells,a,b)
        self.assertEqual(coverage['survey']['classified_n'],30)
        self.assertAlmostEqual(coverage['survey']['classified_weight_pct'],100*60/260)
        self.assertEqual(cells.survey_n.sum(),32)
        self.assertEqual(cells.survey_weight_sum.sum(),260)
        self.assertTrue(cells.loc[cells.survey_n.eq(0),'survey_low_service_pct'].isna().all())

    def test_other_cells_do_not_change_a_cells_low_service_rate(self):
        a=self.residents('survey',30)
        extra=self.residents('survey',60,klass='A',x=2_610_100.)
        extra.HHNR='extra'+extra.HHNR
        a=pd.concat([a,extra],ignore_index=True)
        b=self.residents('synthetic',30)
        grid,geometry=spatial.make_grid(box(2_600_000,1_200_000,2_620_000,1_210_000),10_000)
        cells=access.aggregate_quality_grid(a,b,grid,geometry,10_000,30,30)
        self.assertEqual(cells.loc[cells.ix.eq(260),'survey_low_service_pct'].item(),100.)
        self.assertEqual(cells.loc[cells.ix.eq(261),'survey_low_service_pct'].item(),0.)
        self.assertFalse(cells.loc[cells.ix.eq(261),'difference_shown'].item())

    def test_agreed_grid_and_minimum_support_cannot_be_lowered(self):
        grid,geometry=spatial.make_grid(box(2_600_000,1_200_000,2_610_000,1_210_000),10_000)
        for size,n in [(5000,30),(10000,29)]:
            with self.assertRaises(ValueError):
                access.aggregate_quality_grid(self.residents('survey',30),self.residents('synthetic',30),
                                              grid,geometry,size,n,30)


class SharedCohortFixes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=make_fixture(Path(self.temp.name)/'data')
        self.env=patch.dict(os.environ,{'ABMT_DATA_DIR':str(self.base)})
        self.env.start();self.addCleanup(self.env.stop)
        common._survey.cache_clear();self.addCleanup(common._survey.cache_clear)
        import ABMT_Neuchatel_Step2_Population as s2
        import ABMT_Neuchatel_Step3_SpatialPopulation as s3
        self.s2,self.s3=s2,s3
        for module in [s2,s3]:
            p=patch.object(module,'BASE',self.base);p.start();self.addCleanup(p.stop)
        self.region=age.read_boundary(self.base/'Boundary/neuchatel.shp')[0]
        self.transformer=Transformer.from_crs(21781,2056,always_xy=True)

    def test_steps2_3_exactly_match_shared_eligibility_and_weights(self):
        path=self.base/'microcensus/zielpersonen.csv'
        f=pd.read_csv(path)
        f[['alter','WP']]=f[['alter','WP']].astype(float)
        for i,value in enumerate([5,6,15,16,17,18,34,35,64,65,120,121,30.5,np.nan]):
            f.loc[i,'alter']=value
        for i,value in zip(range(14,20),[0,-1,np.inf,-np.inf,np.nan,2]): f.loc[i,'WP']=value
        f.loc[20,'tag']=6
        f.to_csv(path,index=False)
        expected=common.survey_people()
        a=self.s2.read_microcensus(self.region)
        b=self.s3.read_microcensus(self.region.context)
        self.assertEqual(a.HHNR.tolist(),expected.HHNR.tolist())
        self.assertEqual(b.HHNR.tolist(),expected.HHNR.tolist())
        np.testing.assert_array_equal(a.WP,expected.weight)
        np.testing.assert_array_equal(b.weight,expected.weight)
        for invalid in ['p0','p11','p12','p13','p14','p15','p16','p17','p18','p20']:
            self.assertNotIn(invalid,set(expected.HHNR))
        for valid in ['p1','p2','p3','p4','p5','p6','p7','p8','p9','p10','p19']:
            self.assertIn(valid,set(expected.HHNR))

    def test_new_readers_link_the_complete_shared_cohorts(self):
        survey=common.survey_people()
        enriched=subs.add_extra_fields(survey,self.base)
        assigned=access.add_survey_class(survey,self.base)
        for frame in [enriched,assigned]:
            pd.testing.assert_series_equal(frame.HHNR,survey.HHNR,check_names=False)
            np.testing.assert_array_equal(frame.weight,survey.weight)
        model,_=common.resident_home_attributes(('subscriptions','ovgk'))
        self.assertEqual(set(model.person),common.resident_ids())
        self.assertEqual(model.weight.sum(),120.)
        self.assertIn('p2',set(model.person))  # confirmed nontraveller
        self.assertTrue(model.ovgk.isna().any())

    def test_extra_questionnaire_linkage_must_not_drop_residents(self):
        people=common.survey_people()
        path=self.base/'microcensus/zielpersonen.csv';f=pd.read_csv(path)
        f.iloc[1:].to_csv(path,index=False)
        with self.assertRaisesRegex(ValueError,'cohort'):
            subs.add_extra_fields(people,self.base)

    def block(self, gap):
        return pd.DataFrame([
            ['x',1,1,1,1,600000,200000,601000,200000,15],
            ['x',2,2,1,1,601000+gap,200000,602000,201000,15]],columns=modes.SURVEY_COLUMNS)

    def test_disconnected_transfer_stays_unresolved_in_both_readers(self):
        import _abmt_patterns as patterns
        person=SimpleNamespace(HHNR='x',weight=1.,f50200=1,T_WEGE=2)
        for gap in [2.,1000.,np.nan]:
            rows=self.block(gap)
            count,_,audit=trips.count_survey_person(rows,1,2,self.region,self.transformer)
            self.assertIsNone(count)
            self.assertEqual(audit['discontinuous_transfer_blocks'],1)
            self.assertEqual(audit['internal_trips'],0)
            for reader in [modes.survey_person,patterns.survey_person]:
                modal_audit=modes.Audit()
                self.assertEqual(reader(person,rows.assign(f51100=480),{},self.region,self.transformer,modal_audit),[])
                self.assertEqual(modal_audit.n['discontinuous_transfer_blocks'],1)

    def test_connected_transfer_within_one_metre_counts_once(self):
        for gap in [0.,1.]:
            self.assertEqual(trips.count_survey_person(self.block(gap),1,2,self.region,self.transformer)[0],1)


class FullRunConfiguration(unittest.TestCase):
    def test_default_steps_include14_15_and_archive_previous_results(self):
        import run_all
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);results=root/'results';results.mkdir();(results/'old.txt').write_text('old')
            for step in range(2,16): (root/f'ABMT_Neuchatel_Step{step}_Test.py').write_text('')
            with patch.object(run_all,'__file__',str(root/'run_all.py')),patch.object(sys,'argv',['run_all.py']),patch.object(run_all.subprocess,'run') as runner:
                run_all.main()
            self.assertEqual(runner.call_count,14)
            self.assertIn('Step15_',runner.call_args.args[0][1])
            self.assertFalse(results.exists())
            self.assertEqual(len(list(root.glob('results_previous_*/old.txt'))),1)

    def test_partial_run_does_not_archive_results(self):
        import run_all
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'results').mkdir()
            (root/'ABMT_Neuchatel_Step14_Test.py').write_text('')
            with patch.object(run_all,'__file__',str(root/'run_all.py')),patch.object(sys,'argv',['run_all.py','--steps','14']),patch.object(run_all.subprocess,'run') as runner:
                run_all.main()
            self.assertEqual(runner.call_count,1)
            self.assertTrue((root/'results').is_dir())

    def test_collector_contains_only_requested_aggregate_formats(self):
        from collect_review import collect
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);results=root/'results'
            for folder,name in [('pt_subscriptions','pt_subscriptions_method.json'),
                                ('pt_accessibility_common','pt_accessibility_common_method.json')]:
                directory=results/folder;directory.mkdir(parents=True)
                (directory/name).write_text('{}')
            (results/'comparison.csv').write_text('category,n\nA,30\n')
            (results/'figure.png').write_bytes(b'artificial figure')
            (results/'raw.xml').write_text('do not collect')
            (results/'unrelated.json').write_text('{}')
            (root/'private_input.csv').write_text('do not collect')
            with zipfile.ZipFile(collect(root)) as archive:
                self.assertEqual(len(archive.namelist()),4)
                self.assertTrue(all(name.startswith('results/') for name in archive.namelist()))
                self.assertFalse(any('raw' in name or 'unrelated' in name or 'private' in name for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
