"""Regression tests for the agreed analysis definitions, using artificial data only."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from collections import Counter
import xml.etree.ElementTree as ET

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from make_fixture import make_fixture
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import box


class AgreedRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.base=make_fixture(Path(cls.temp.name)/'neuchatel-data')
        os.environ['ABMT_DATA_DIR']=str(cls.base)
        import abmt_common as c
        import _abmt_modes as m
        import _abmt_trips as t
        import _abmt_chains as ch
        import _abmt_visits as v
        import _abmt_pt as pt
        import ABMT_Neuchatel_Step2_Population as s2
        import ABMT_Neuchatel_Step3_SpatialPopulation as s3
        import ABMT_Neuchatel_Step5_SpatialActivities as s5
        import ABMT_Neuchatel_Step6_ModeShare as s6
        import ABMT_Neuchatel_Step7_ModeShareDistance as s7
        import ABMT_Neuchatel_Step10_PT_GEH as s10
        cls.c,cls.m,cls.t,cls.ch,cls.v,cls.pt=c,m,t,ch,v,pt
        cls.s2,cls.s3,cls.s5,cls.s6,cls.s7,cls.s10=s2,s3,s5,s6,s7,s10
        c.OUT=Path(cls.temp.name)/'results'
        cls.region=c.boundary();cls.ids=c.resident_ids()
        cls.transformer=Transformer.from_crs(21781,2056,always_xy=True)
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def plan(self,acts,modes):
        plan=ET.Element('plan',selected='yes')
        for i,(kind,x,y) in enumerate(acts):
            ET.SubElement(plan,'act',type=kind,x=str(x),y=str(y))
            if i<len(modes):ET.SubElement(plan,'leg',mode=modes[i])
        return plan

    def test_residents_with_outside_flags_are_retained(self):
        self.assertIn('p0',self.ids); self.assertIn('p6',self.ids)
        self.assertNotIn('external',self.ids); self.assertNotIn('freight',self.ids)
        self.assertEqual(len(self.ids),120)
    def test_population_cohorts_match(self):
        self.assertEqual(len(self.s2.read_population(self.s2.read_boundary())),120)
        self.assertEqual(len(self.s3.read_population(self.s3.read_boundary())),120)
    def test_age_classes(self):
        import _abmt_patterns as p
        self.assertEqual([p.age_group(x) for x in [6,17,18,34,35,50,64,65,120]],
                         ['6-17','6-17','18-34','18-34','35-64','35-64','35-64','65+','65+'])
    def test_eqasim_mode_grouping(self):
        self.assertEqual([self.m.survey_mode(x,set()) for x in [1,11,12,13,16]],['pt','pt','car','car','bike'])
    def test_passenger_sensitivity_keeps_pt_main_mode(self):
        self.assertEqual(self.m.survey_mode(2,{8,9}),'car_passenger')
        self.assertEqual(self.m.survey_mode_strict(2,{8,9}),'pt')
    def test_strict_car_role_missing_or_mixed_is_unknown(self):
        self.assertEqual(self.m.survey_mode_strict(9,set()),'unknown')
        self.assertEqual(self.m.survey_mode_strict(9,{7,8}),'unknown')
    def test_primary_selection_excludes_loop_remote_unknown_zero(self):
        f=pd.DataFrame(dict(mode=['walk','walk','other','unknown','walk'],loop=[False,True,False,False,False],
                            code=['15','walk_loop','remote_walk','17','15'],distance_m=[20,0,0,50,0]))
        self.assertEqual(self.c.primary_mask(f).tolist(),[True,False,False,False,False])
    def test_household_ownership_and_personal_access_are_distinct(self):
        a,b=self.c.person_attributes()
        self.assertTrue(((a.car_group=='0 cars')&(a.personal=='available')).any())
        self.assertTrue(((a.personal=='available')&(a.personal_always_only=='unavailable')).any())
    def test_all_mode_steps_use_same_base_trips(self):
        a,b,_=self.c.mode_data()
        sa,_,_=self.s6.read_survey_trips(self.c.survey_people())
        sb,_,_,_=self.s6.read_sim_trips(self.ids)
        da,_=self.s7.read_mz_distance_trips(self.c.survey_people())
        db,_=self.s7.read_matsim_distance_trips(self.ids)
        pd.testing.assert_frame_equal(a,sa);pd.testing.assert_frame_equal(b,sb)
        pd.testing.assert_frame_equal(a,da);pd.testing.assert_frame_equal(b,db)
    def test_survey_outside_barrier_does_not_create_trip(self):
        rows=pd.DataFrame([
          ['x',1,13,1,1,600000,200000,625000,200000],
          ['x',2,2,1,1,625000,200000,602000,201000],
          ['x',3,11,2,1,602000,201000,600000,200000]],columns=self.t.TRIP_COLUMNS)
        n,state,audit=self.t.count_survey_person(rows,1,3,self.region,self.transformer)
        self.assertEqual(n,1);self.assertEqual(state,'travel');self.assertEqual(audit['excluded_outside_trips'],2)
    def test_model_outside_barrier_does_not_create_trip(self):
        h=self.transformer.transform(600000,200000);d=self.transformer.transform(602000,201000)
        p=self.plan([('home',*h),('outside',*h),('work',*d),('home',*h)],['walk','outside','walk'])
        n,state,audit=self.t.count_model_plan(p,self.region)
        self.assertEqual(n,1);self.assertEqual(state,'travel')
    def test_unknown_location_does_not_become_zero(self):
        h=self.transformer.transform(600000,200000)
        p=self.plan([('home',*h),('work','NaN','NaN'),('home',*h)],['walk','walk'])
        self.assertIsNone(self.t.count_model_plan(p,self.region)[0])
    def test_confirmed_home_only_zero(self):
        h=self.transformer.transform(600000,200000)
        self.assertEqual(self.t.count_model_plan(self.plan([('home',*h)],[]),self.region)[:2],(0,'no_travel'))
    def test_loop_is_travel(self):
        h=self.transformer.transform(600000,200000)
        self.assertEqual(self.t.count_model_plan(self.plan([('home',*h),('home',*h)],['walk_loop']),self.region,True)[:2],(1,'travel'))
    def test_remote_walk_not_counted_as_physical_travel(self):
        h=self.transformer.transform(600000,200000)
        p=self.plan([('home',*h),('work',*h),('home',*h)],['remote_walk','remote_walk'])
        n,state,audit=self.t.count_model_plan(p,self.region)
        self.assertIsNone(n);self.assertEqual(state,'unknown')
    def test_no_way_record_alone_does_not_prove_home_only(self):
        empty=pd.DataFrame(columns=self.t.TRIP_COLUMNS)
        from types import SimpleNamespace
        p=SimpleNamespace(f50200=1,T_WEGE=2)
        chain,_=self.ch.survey_chain(p,empty,[],self.region,self.transformer,30)
        self.assertEqual(chain,self.ch.UNKNOWN)
    def test_initial_nonhome_activity_is_not_assumed_home(self):
        from types import SimpleNamespace
        p=SimpleNamespace(W_X_CH1903=600000,W_Y_CH1903=200000,A_X_CH1903=602000,A_Y_CH1903=201000,
                          AU_X_CH1903=None,AU_Y_CH1903=None)
        trip=SimpleNamespace(S_X_CH1903=602000,S_Y_CH1903=201000)
        self.assertEqual(self.ch.initial_survey_purpose(p,trip,[],30)[0],'W')
    def test_initial_work_is_not_an_arrival(self):
        h=self.transformer.transform(600000,200000);w=self.transformer.transform(602000,201000)
        p=ET.Element('person');p.append(self.plan([('work',*w),('home',*h)],['walk']))
        found,state,audit=self.v.model_visits(p,'x','available',self.region)
        self.assertEqual(found,[])
    def test_inside_destination_can_have_outside_origin(self):
        h=self.transformer.transform(600000,200000);w=self.transformer.transform(602000,201000)
        p=ET.Element('person');p.append(self.plan([('outside',*h),('work',*w)],['walk']))
        found,state,audit=self.v.model_visits(p,'x','available',self.region)
        self.assertEqual(len(found),1)
    def test_chain_denominator_keeps_all_residents(self):
        a,b=self.c.chain_data()
        self.assertEqual(len(a),120);self.assertEqual(len(b),120)
        self.assertIn('outside_chain',set(a.chain));self.assertIn('outside_chain',set(b.chain))
    def test_map_threshold_counts_people_not_visits(self):
        x,y=self.transformer.transform(600000,200000)
        a=pd.DataFrame([dict(person_id=str(i%29),weight=1,x=x,y=y) for i in range(290)])
        b=pd.DataFrame([dict(person_id=str(i),weight=1,x=x,y=y) for i in range(30)])
        table,_=self.s5.grid_table(a,b,box(x-100,y-100,x+100,y+100),10000)
        self.assertFalse(bool(table.comparable.iloc[0]))
        a.loc[len(a)]=dict(person_id='29',weight=1,x=x,y=y)
        table,_=self.s5.grid_table(a,b,box(x-100,y-100,x+100,y+100),10000)
        self.assertTrue(bool(table.comparable.iloc[0]))
    def test_gehlane_and_link_scales(self):
        table=pd.DataFrame(dict(comparison_lanes=[2.],model_average_hourly_link_vehicles=[120.],observed_average_hourly_link_vehicles=[100.]))
        lane=self.s10.add_lane_sensitivity(table).iloc[0]
        self.assertAlmostEqual(self.pt.geh(120,100),np.sqrt(2)*lane.geh_average_hourly_per_lane)
    def test_bus_sample_weight_and_daily_reconciliation(self):
        t,s=self.pt.road_summary({'L':50},{'L':dict(status='eligible',network_lanes=2,comparison_lanes=2)},
             {'L':Counter(car=10,bus=1)},{'L':10.1},.1,0,86400,'hourly_per_lane',[])
        self.assertAlmostEqual(t.model_average_hourly_link_vehicles.iloc[0],10.1/.1/24)
        self.assertTrue(t.daily_file_matches_sample_count.iloc[0]);self.assertEqual(t.observed_average_hourly_link_vehicles.iloc[0],100)
    def test_geh_both_zero(self): self.assertEqual(self.pt.geh(0,0),0)

    def test_weighted_trip_denominator(self):
        a,b,_=self.c.mode_data()
        self.assertEqual(len(a),230);self.assertEqual(len(b),230)
        self.assertAlmostEqual(a.WP.sum(),458.)

    def test_transfer_rows_form_one_trip_with_unknown_mixed_main_mode(self):
        import _abmt_patterns as p
        from types import SimpleNamespace
        rows=pd.DataFrame([
          ['x',1,1,1,1,600000,200000,601000,200000,15,480],
          ['x',2,2,1,1,601000,200000,602000,201000,2,490]],columns=p.SURVEY_COLUMNS)
        hx,hy=self.transformer.transform(600000,200000)
        person=SimpleNamespace(HHNR='x',weight=1.,f50200=1,T_WEGE=2,person_key='x',
          age_group='18-34',personal='available',home_x=hx,home_y=hy)
        audit=self.m.Audit()
        found=p.survey_person(person,rows,{('x',1):{1},('x',2):{9}},self.region,self.transformer,audit)
        self.assertEqual(len(found),1);self.assertEqual(found[0]['mode'],'unknown')
        self.assertEqual(audit.n['transfer_destinations_collapsed'],1)

    def test_grid_lattice_matches_between_activity_and_mode_maps(self):
        import ABMT_Neuchatel_Step9_SpatialTemporalModeShare as s9
        x,y=self.transformer.transform(600000,200000)
        polygon=box(x-1234,y-4321,x+11000,y+9000)
        f=pd.DataFrame([dict(person_id='x',weight=1.,x=x,y=y)])
        t,_=self.s5.grid_table(f,f,polygon,10000)
        minx=np.floor(polygon.bounds[0]/10000)*10000;miny=np.floor(polygon.bounds[1]/10000)*10000
        h=s9.grid_cells(pd.DataFrame([dict(home_x=x,home_y=y)]),minx,miny)
        self.assertEqual((int(t.ix.iloc[0]),int(t.iy.iloc[0])),(int(h.ix.iloc[0]),int(h.iy.iloc[0])))

    def test_event_reader_excludes_drivers_and_outside_stops(self):
        sim=self.base/'Neuchatel_10pct/simulation_output'
        stops,routes,vehicles,deps=self.pt.read_schedule(sim/'output_transitSchedule.xml.gz',self.region)
        counts,delays,road,audit=self.pt.event_analysis(sim/'output_events.xml.gz',stops,routes,vehicles,deps,{'L1'},.1,0,86400)
        self.assertEqual(counts[('all','all','boardings_n')],1)
        self.assertEqual(counts[('all','all','alightings_n')],1)
        self.assertFalse(stops['S3']);self.assertEqual(road['L1']['car'],10)


if __name__=='__main__': unittest.main()
