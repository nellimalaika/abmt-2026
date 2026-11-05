"""Shared definitions for the reconciled Neuchatel analysis (5 November 2026).

Only aggregate outputs are written. Individual records remain in local memory.
The named Step scripts are the supported entry points; _abmt_* are helpers.
"""
from collections import Counter
from functools import lru_cache
from pathlib import Path
import gzip
import json
import math
import xml.etree.ElementTree as ET

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer

from abmt_paths import data_root
import _abmt_age as age
import _abmt_car as car
import _abmt_patterns as patterns
import _abmt_modes as modes
import _abmt_trips as trips
import _abmt_chains as chains
import _abmt_visits as visits

VERSION = 'Neuchatel reconciled 2026-11-05 v1.1'
OUT = Path(__file__).resolve().parent/'results'
AGE_GROUPS = ['6-17','18-34','35-64','65+']
MODES = ['car','car_passenger','pt','bike','walk']
GRID_M = 10_000
MIN_CONTRIBUTORS = 30
METHOD = {
 'version':VERSION,
 'residents':'Actual home inside Boundary/neuchatel.shp; integer ages 6-120; freight excluded; other outside/cross-border flags are diagnostic only.',
 'survey':'Monday-Friday; positive finite person weight WP. Each retained trip carries WP.',
 'trip_scope':'Both substantive endpoints inside. Artificial outside/border activities and modes are barriers, not removed to join neighbours. Connected transfer rows are collapsed; mixed main modes remain unknown.',
 'main_modal_split':'Five eqasim categories; no loop, remote_walk, unknown/other or straight-line distance <=1 m. Exactly the same base selection in Steps6-9.',
 'mode_mapping':'eqasim cmdp_new_version data/microcensus/trips.py; any car-passenger stage gives car_passenger. strict sensitivity preserves the survey main mode and resolves only car main trips by roles.',
 'distance':'Euclidean distance between substantive endpoints in LV95, for BOTH sources; not network distance.',
 'age_groups':AGE_GROUPS,
 'car_access':'Household ownership and personal driver access are distinct. Main survey access=always or after agreement; always-only sensitivity also saved.',
 'grid':'10 km, anchored at LV95 origin (0,0), clipped to boundary; differences shown only with >=30 distinct people per source.',
 'trips_per_resident':'Synthetic input selected plans vs survey diaries, all physical modes and identified loops. Unknown counts stay unknown. Remote-walk plans are unresolved for physical trip-frequency inference.',
 'chains':'All eligible residents in denominator; confirmed home-only, H-H travel, outside, unknown and other chains separate. Initial purpose verified. Work and business grouped consistently as work.',
 'destinations':'Arrivals at known non-home activities inside; initial activities excluded; origin may be outside; repeats count repeatedly.',
 'road':'All traffic at eligible inside-midpoint target links; average-hourly whole-link GEH primary, per-lane supplementary; event counts checked against supplied daily counts.',
 'pt':'Simulated passenger activity and timetable adherence are separate diagnostics, not validation against observed passenger demand or punctuality.',
 'sources':[
  'https://github.com/nellimalaika/abmt-2026/tree/ad3d05911c3ea1141fb034abb9b7db19836886fc/assignment2_analysis',
  'https://github.com/eqasim-org/eqasim-switzerland/blob/cmdp_new_version/data/microcensus/trips.py',
  'https://github.com/eqasim-org/eqasim-switzerland/blob/cmdp_new_version/analysis/mode_shares/utils.py',
 ],
}

def output(name):
    p=OUT/name
    p.mkdir(parents=True,exist_ok=True)
    return p

def write_method(folder, extra=None):
    def clean(x):
        if isinstance(x,dict): return {str(k):clean(v) for k,v in x.items()}
        if isinstance(x,(list,tuple)): return [clean(v) for v in x]
        if isinstance(x,np.generic): x=x.item()
        if isinstance(x,float) and not math.isfinite(x): return None
        return x
    Path(folder).mkdir(parents=True,exist_ok=True)
    (Path(folder)/'reconciled_method.json').write_text(
        json.dumps(clean(METHOD | (extra or {})),indent=2,allow_nan=False)+'\n',encoding='utf8')

def boundary():
    return age.read_boundary(age.unique_file(data_root(),'neuchatel.shp'))[0]

def population_path():
    return age.unique_file(data_root(),'neuchatel_population.xml.gz')

@lru_cache(maxsize=1)
def _survey():
    return age.read_survey(data_root(),boundary())

def survey_people(region=None):
    if region is None: return _survey()[0].copy()
    return age.read_survey(data_root(),region)[0]

def resident_ids(region=None):
    return modes.read_resident_ids(population_path(),region or boundary())[0]

@lru_cache(maxsize=1)
def person_attributes():
    people=survey_people()
    people['age_group']=people.age.map(patterns.age_group)
    people['car_group']=people.f30100.map(lambda v:'0 cars' if age.car_count_status(v)=='0_cars' else
        '1+ cars' if age.car_count_status(v)=='1plus_cars' else 'unknown')
    people['personal']=people.f42100e.map(lambda v:car.survey_access(v,'any_access'))
    people['personal_always_only']=people.f42100e.map(lambda v:car.survey_access(v,'always_only'))
    t=Transformer.from_crs(21781,2056,always_xy=True)
    people['home_x'],people['home_y']=t.transform(people.W_X_CH1903.to_numpy(),people.W_Y_CH1903.to_numpy())
    eligible=resident_ids()
    rows=[]
    with gzip.open(population_path(),'rb') as stream:
        root=None
        for event,p in ET.iterparse(stream,events=('start','end')):
            if root is None: root=p
            if event!='end' or trips.local_name(p)!='person': continue
            pid=p.get('id')
            if pid in eligible:
                attrs={a.get('name'):(a.text or '').strip() for section in p
                       if trips.local_name(section)=='attributes' for a in section
                       if trips.local_name(a)=='attribute'}
                xy=[(age.number(attrs.get(a)),age.number(attrs.get(b))) for a,b in
                    [('home_coordinate_x','home_coordinate_y'),('home_x','home_y')]]
                x,y=xy[0 if all(v is not None for v in xy[0]) else 1]
                count=age.car_count_status(attrs.get('numberOfCars'))
                own='0 cars' if count=='0_cars' else '1+ cars' if count=='1plus_cars' else 'unknown'
                rows.append(dict(person=pid,age=age.number(attrs.get('age')),car_group=own,
                    personal=car.synthetic_access(attrs.get('carAvail','').casefold()),home_x=x,home_y=y))
            root.clear()
    sim=pd.DataFrame(rows)
    if len(sim)!=len(eligible) or sim.person.duplicated().any():
        raise ValueError('Resident attribute linkage does not reconcile.')
    sim['age_group']=sim.age.map(patterns.age_group)
    sim['personal_always_only']=sim.personal
    return people[['HHNR','age','age_group','car_group','personal','personal_always_only','home_x','home_y']],sim

def primary_mask(frame):
    return (frame['mode'].isin(MODES) & ~frame['loop'].fillna(False).astype(bool)
            & frame.code.ne('remote_walk') & pd.to_numeric(frame.distance_m,errors='coerce').gt(1)
            & np.isfinite(pd.to_numeric(frame.distance_m,errors='coerce')))

def coverage(frame,source):
    reasons=np.select([
        frame.code.eq('remote_walk'),frame['loop'].fillna(False).astype(bool),
        ~frame['mode'].isin(MODES),~np.isfinite(pd.to_numeric(frame.distance_m,errors='coerce')),
        pd.to_numeric(frame.distance_m,errors='coerce').le(1)],
        ['remote_walk','loop','other_or_unknown_mode','unknown_distance','coincident_or_short'],default='included_primary')
    c=frame.assign(selection_status=reasons)
    out=c.groupby('selection_status',dropna=False).agg(n=('weight','size'),weight_sum=('weight','sum')).reset_index()
    out['source']=source
    out['pct_internal_trip_weight']=100*out.weight_sum/c.weight.sum() if c.weight.sum() else np.nan
    return out

@lru_cache(maxsize=2)
def mode_data(definition='eqasim'):
    if definition not in {'eqasim','strict'}: raise ValueError('Invalid mode definition.')
    region=boundary()
    previous=patterns.survey_mode
    patterns.survey_mode=modes.survey_mode if definition=='eqasim' else modes.survey_mode_strict
    try: mz,audit,selection=patterns.read_survey(data_root(),region)
    finally: patterns.survey_mode=previous
    ids,sim_selection=patterns.read_residents(population_path(),region)
    sim,sim_audit=patterns.read_simulation(age.unique_file(data_root(),'output_trips.csv.gz'),ids,region)
    cov=pd.concat([coverage(mz,'survey'),coverage(sim,'simulation')],ignore_index=True)
    folder=output('mode_share')
    cov.to_csv(folder/f'trip_selection_coverage_{definition}.csv',index=False)
    pd.DataFrame(audit.table('survey')+sim_audit.table('simulation')).to_csv(folder/f'trip_audit_{definition}.csv',index=False)
    report={'mode_definition':definition,'survey_selection':selection,'simulation_selection':sim_selection,
      'survey_internal_n':len(mz),'simulation_internal_n':len(sim),
      'survey_excluded_primary_n':int((~primary_mask(mz)).sum()),
      'simulation_excluded_primary_n':int((~primary_mask(sim)).sum()),
      'simulation_loop_n':int(sim['loop'].sum()),'coverage':cov.to_dict('records')}
    a=mz.loc[primary_mask(mz)].copy(); b=sim.loc[primary_mask(sim)].copy()
    a=a.rename(columns={'person_key':'HHNR','weight':'WP','departure_seconds':'time_seconds'})
    b=b.rename(columns={'person_key':'person','departure_seconds':'time_seconds'})
    # Identical base rows feed overall, distance, demographic, time and spatial shares.
    for f in (a,b):
        if f.empty: raise ValueError('No conventional internal trips satisfy the primary definition; inspect aggregate trip_selection_coverage files.')
    return (a[['HHNR','WP','mode','distance_m','time_seconds']],
            b[['person','mode','distance_m','time_seconds']],report)

def check_partition(original, selected, key, weight=None):
    if sum(len(x) for x in selected)!=len(original): raise AssertionError('Trip partition is incomplete.')
    if weight and not np.isclose(sum(x[weight].sum() for x in selected),original[weight].sum()):
        raise AssertionError('Trip weights do not reconcile.')

@lru_cache(maxsize=1)
def chain_data():
    region=boundary()
    a,am=chains.read_survey(data_root(),region,30.)
    b,bm=chains.read_population(population_path(),region)
    write_method(output('activity_chains'),{'survey_chain_audit':am,'model_chain_audit':bm})
    return a,b

def chain_label(s):
    if s=='H': return 'Home only (confirmed)'
    if s=='H-H': return 'Home-to-home travel / loop'
    return {'outside_chain':'Outside travel','unknown_chain':'Unknown / incomplete',
            'other_chains':'Other classified chains'}.get(s,s)

def compare_categories(a,b,column,categories,denominator='all eligible residents'):
    rows=[]
    for cat in categories:
        row={column:cat}
        for source,frame in [('microcensus',a),('matsim',b)]:
            group=frame.loc[frame[column].eq(cat)]
            row[source+'_n']=len(group)
            row[source+'_weight_sum']=float(group.weight.sum())
            row[source+'_pct']=100*group.weight.sum()/frame.weight.sum() if frame.weight.sum()>0 else np.nan
        row['difference_pp']=row['matsim_pct']-row['microcensus_pct']
        rows.append(row)
    t=pd.DataFrame(rows)
    for source,frame in [('microcensus',a),('matsim',b)]:
        if not frame.empty and (t[source+'_n'].sum()!=len(frame) or not np.isclose(t[source+'_pct'].sum(),100)):
            raise AssertionError('Category totals do not cover the selected residents.')
    return t

def plot_categories(table,column,path,title,xlabel):
    fig,ax=plt.subplots(figsize=(11,max(5,.43*len(table)+2)))
    y=np.arange(len(table))
    ax.barh(y-.18,table.microcensus_pct,.36,label='Mikrozensus (WP weighted)')
    ax.barh(y+.18,table.matsim_pct,.36,label='Synthetic input plans (10% sample)')
    labels=table[column].map(chain_label) if column=='chain' else table[column]
    ax.set(yticks=y,yticklabels=labels,xlabel=xlabel,title=title)
    ax.invert_yaxis(); ax.grid(axis='x',alpha=.2); ax.legend()
    fig.tight_layout(); fig.savefig(path,dpi=170); plt.close(fig)

def run_chains():
    a,b=chain_data(); folder=output('activity_chains')
    for cohort,minimum in [('all',6),('age65plus',65)]:
        x=a.loc[a.age.ge(minimum)].copy(); y=b.loc[b.age.ge(minimum)].copy()
        for conditional in (False,True):
            p,q=x.copy(),y.copy()
            if conditional:
                def mobile(s): return s.str.startswith('H-') & s.str.endswith('-H') | s.eq('H-H')
                p=p.loc[mobile(p.chain)]; q=q.loc[mobile(q.chain)]
            ranked=p.loc[~p.chain.isin(['H','outside_chain','unknown_chain'])].groupby('chain').weight.sum().sort_values(ascending=False).head(10).index.tolist()
            cats=([] if conditional else ['H'])+ranked+['other_chains']+([] if conditional else ['outside_chain','unknown_chain'])
            for frame in [p,q]: frame['chain']=frame.chain.where(frame.chain.isin(cats),'other_chains')
            t=compare_categories(p,q,'chain',cats)
            suffix='_mobile_conditional' if conditional else ''
            t.to_csv(folder/f'activity_chains_{cohort}{suffix}.csv',index=False)
            plot_categories(t,'chain',folder/f'activity_chains_{cohort}{suffix}.png',
              f'Neuchatel activity chains: {cohort}',
              'Share of classifiable closed mobile chains (%)' if conditional else 'Share of all eligible residents (%)')
        p=x.assign(structure=x.chain.map(chains.structure)); q=y.assign(structure=y.chain.map(chains.structure))
        compare_categories(p,q,'structure',chains.STRUCTURES).to_csv(folder/f'activity_chain_structure_{cohort}.csv',index=False)

def run_trip_frequency():
    folder=output('trips_per_person'); region=boundary()
    a,am=trips.read_survey_counts(data_root(),region)
    b,bm=trips.read_population_counts(population_path(),region)
    for cohort,minimum in [('all',6),('age65plus',65)]:
        x=a.loc[a.age.ge(minimum)].copy(); y=b.loc[b.age.ge(minimum)].copy()
        for f in [x,y]: f['trips']=f.trip_count.map(lambda v:'unknown' if pd.isna(v) else str(int(v)) if v<8 else '8+')
        cats=[str(i) for i in range(8)]+['8+','unknown']
        t=compare_categories(x,y,'trips',cats)
        t.to_csv(folder/f'trips_per_person_{cohort}.csv',index=False)
        plot_categories(t,'trips',folder/f'trips_per_person_{cohort}.png',f'Internal physical trips per resident: {cohort}',
                        'Share of all eligible residents (%)')
        tab,summary=trips.compare(x,y)
        summary.to_csv(folder/f'trip_frequency_summary_{cohort}.csv',index=False)
    write_method(folder,{'survey_trip_count_audit':am,'model_trip_count_audit':bm})

def run_home_check():
    a,b=chain_data(); folder=output('home_only_check')
    cats=['confirmed_home_only','home_home_travel','outside_travel','unknown_or_incomplete','other_activity_chain']
    def status(s): return ('confirmed_home_only' if s=='H' else 'home_home_travel' if s=='H-H' else
        'outside_travel' if s==chains.OUTSIDE else 'unknown_or_incomplete' if s==chains.UNKNOWN else 'other_activity_chain')
    for cohort,minimum in [('all',6),('age65plus',65)]:
        x=a.loc[a.age.ge(minimum)].assign(status=lambda f:f.chain.map(status))
        y=b.loc[b.age.ge(minimum)].assign(status=lambda f:f.chain.map(status))
        t=compare_categories(x,y,'status',cats)
        t.to_csv(folder/f'home_only_coverage_{cohort}.csv',index=False)
        plot_categories(t,'status',folder/f'home_only_coverage_{cohort}.png',
          f'Home-only and chain ascertainment: {cohort}','Share of all eligible residents (%)')
    write_method(folder,{'home_only_rule':'Only confirmed nontravel response or a single-home zero-leg input plan. No-way records alone are not home-only. Other activity chains are not necessarily physical travel (homeoffice).'} )

@lru_cache(maxsize=1)
def visit_data():
    region=boundary()
    a,ap,am=visits.read_survey(data_root(),region)
    b,bp,bm=visits.read_population(population_path(),region)
    pa,pb=person_attributes()
    purposes={'W':'work','B':'work','E':'education','S':'shop','L':'leisure','O':'other'}
    result=[]
    for f,p,key,attrs in [(a,ap,'HHNR',pa),(b,bp,'person',pb)]:
        f=f.copy(); f['purpose']=f.purpose.map(purposes)
        f=f.merge(attrs[[key,'age']].rename(columns={key:'person_key'}),on='person_key',validate='many_to_one')
        result.append(f.rename(columns={'person_key':'person_id'}))
    folder=output('spatial_activities')
    report=[]
    for source,f,p in [('survey',a,ap),('simulation',b,bp)]:
        report.append({'source':source,'visits_n':len(f),'visit_weight_sum':float(f.weight.sum()),
          'resident_weight_sum':float(p.weight.sum()),
          'visits_per_100_residents':100*f.weight.sum()/p.weight.sum() if p.weight.sum() else None})
    pd.DataFrame(report).to_csv(folder/'destination_coverage.csv',index=False)
    write_method(folder,{'survey_visit_audit':am,'model_visit_audit':bm})
    return tuple(result)

def run_car_access():
    folder=output('car_access')
    a=survey_people(); b,_=age.read_population(population_path(),boundary())
    for definition in ['any_access','always_only']:
        table=car.compare(a,b,definition)
        table.to_csv(folder/f'household_ownership_x_personal_access_{definition}.csv',index=False)
        car.plot_comparison(table,definition,len(a),len(b),folder/f'household_ownership_x_personal_access_{definition}.png')
    write_method(folder)
