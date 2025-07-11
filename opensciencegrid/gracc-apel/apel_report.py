#!/usr/bin/env python3

# GRACC-based APEL reporting script; run from docker-run.sh

#import logging
import opensearchpy
from opensearchpy import Search, A, Q
import datetime
import dateutil.relativedelta
import operator
import sys
import os
import requests
import json
from statistics import mean
from math import isclose


#logging.basicConfig(level=logging.WARN)
es = opensearchpy.OpenSearch(
        ['https://gracc.opensciencegrid.org/q'],
        timeout=300, use_ssl=True, verify_certs=True,
        ca_certs='/etc/ssl/certs/ca-bundle.crt')

osg_raw_index = 'gracc.osg.raw-*'
osg_summary_index = 'gracc.osg.summary'

vo_list = ['atlas', 'alice', 'belle', 'cms', 'enmr.eu', 'lhcb']

MAXSZ=2**30
MISSING='__MISSING__'

resource_group_map = None

def get_hs23_portion(resource_group) -> float:
    """
    Download the HS23 portion of the OSG site info from OIM.

    :param resource_group: The Topology resource group name.
    :return: The HS23 portion of the site, or 0.0 if not found.
    """
    global resource_group_map
    if resource_group_map == None:
        # Download the map from Topology
        resp = requests.get("https://topology.opensciencegrid.org/api/resource_group_summary")
        if resp.status_code != 200:
            #print("Error downloading resource group summary from Topology: {}".format(resp.status_code))
            #return 0.0
            raise Exception("Error downloading resource group summary from Topology: {}".format(resp.status_code))
        
        raw_json = resp.json()
        # Parse the JSON response
        resource_group_map = {}
        for resource_group_name in raw_json:
            hep_spec_percentages = []
            for resource in raw_json[resource_group_name]["Resources"]['Resource']:
                if 'HEPScore23Percentage' in resource['WLCGInformation']:
                    hep_spec_percentages.append(float(resource['WLCGInformation']['HEPScore23Percentage']))
            if len(hep_spec_percentages) > 0:
                resource_group_map[resource_group_name] = mean(hep_spec_percentages)
            else:
                resource_group_map[resource_group_name] = 0.0

    return resource_group_map.get(resource_group, 0.0)
        



def add_bkt_metrics(bkt):
    bkt = bkt.metric('NormalFactor','terms', field='OIM_WLCGAPELNormalFactor')
    bkt = bkt.metric('CpuDuration_system', 'sum', field='CpuDuration_system')
    bkt = bkt.metric('CpuDuration_user',   'sum', field='CpuDuration_user')
    bkt = bkt.metric('CpuDuration',        'sum', field='CpuDuration')
    bkt = bkt.metric('WallDuration',       'sum', field='WallDuration')
    bkt = bkt.metric('NumberOfJobs',       'sum', field='Count')
    bkt = bkt.metric('EarliestEndTime',    'min', field='EndTime')
    bkt = bkt.metric('LatestEndTime',      'max', field='EndTime')
    return bkt

def gracc_query_apel(year, month):
    index = osg_summary_index
    starttime = datetime.datetime(year, month, 1)
    onemonth = dateutil.relativedelta.relativedelta(months=1)
    endtime = starttime + onemonth
    s = Search(using=es, index=index)
    s = s.query('bool',
        filter=[
            Q('range', EndTime={'gte': starttime, 'lt': endtime })
          & Q('terms', VOName=vo_list)
          & ( Q('term', ResourceType='Batch')
            | ( Q('term', ResourceType='Payload')
              & Q('term', Grid='Local') )
            )
        ]
    )

    bkt = s.aggs
    bkt = bkt.bucket('Cores', 'terms', size=MAXSZ, field='Processors')
    bkt = bkt.bucket('VO',    'terms', size=MAXSZ, field='VOName')
    bkt = bkt.bucket('DN',    'terms', size=MAXSZ, field='DN')
    bkt = bkt.bucket('Site',  'terms', size=MAXSZ, missing=MISSING, field='OIM_ResourceGroup')
    #bkt = bkt.bucket('Site', 'terms', size=MAXSZ, field='SiteName')
    #bkt = bkt.bucket('Site', 'terms', size=MAXSZ, field='WLCGAccountingName')
    add_bkt_metrics(bkt)

    bkt = bkt.bucket('SiteName',  'terms', size=MAXSZ, field='SiteName')

    add_bkt_metrics(bkt)

    response = s.execute()
    return response

# Fixed entries:
fixed_header = "APEL-normalised-summary-message: v0.4"
fixed_separator = "%%"
fixed_infrastructure = "grid"
fixed_nodecount = 1
fixed_normalizationfactor = 12

def normal_hepspec_table():
    from os.path import join, dirname, abspath
    normal_hepspec_path = join(dirname(abspath(__file__)), "normal_hepspec")
    table = {}
    for line in open(normal_hepspec_path):
        if line.startswith('#'):
            continue
        tokens = line.split()
        if len(tokens) != 2:
            continue
        site, nf = tokens
        nf = float(nf)
        table[site] = nf
    return table

nf_table = normal_hepspec_table()

def norm_factor(bkt, site):
    nf_max = 200
    nf_default = 12
    nf_values = [ b.key for b in bkt.NormalFactor.buckets if b.key > 0 ]
    if len(nf_values) == 0:
        # XXX: *should* look up from table here, but the old script just
        #      used the default (12) when not found on OIM.
        # TODO: log
        nf = nf_default
    elif len(nf_values) == 1:
        # ok, normal case
        nf = nf_values[0]
    else:
        # oh weird, why more than one norm factor here?
        # TODO: log
        nf = 1.0 * sum(nf_values) / len(nf_values)

    if nf >= nf_max:
        # out of range: do table lookup
        # TODO: log
        if site in nf_table:
            # TODO: log
            nf = nf_table[site]
        else:
            # TODO: log
            nf = nf_default
    return nf

from collections import namedtuple, defaultdict

class autodict(defaultdict):
    def __init__(self,*other):
        defaultdict.__init__(self, self.__class__, *other)
    def __add__ (self, other):
        return other
    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, dict.__repr__(self))

RecordKey = namedtuple('RecordKey', ['vo', 'site', 'cores', 'dn'])
Record = namedtuple('Record', ["mintime", "maxtime", "walldur", "cpudur",
                               "nf", "njobs"])

def bkt_record(bkt, site):
    mintime = int(bkt.EarliestEndTime.value / 1000)
    maxtime = int(bkt.LatestEndTime.value / 1000)
    walldur = int(bkt.WallDuration.value)
    if bkt.CpuDuration_user.value == 0 and bkt.CpuDuration_system.value == 0:
        cpudur = int(bkt.CpuDuration.value)
    else:
        cpudur  = int(bkt.CpuDuration_user.value + bkt.CpuDuration_system.value)
    nf      = norm_factor(bkt, site)
    njobs   = int(bkt.NumberOfJobs.value)
    return Record(mintime, maxtime, walldur, cpudur, nf, njobs)

def record_adder(a,b):
    mintime = min(a.mintime, b.mintime)
    maxtime = max(a.maxtime, b.maxtime)
    walldur = a.walldur + b.walldur
    cpudur  = a.cpudur  + b.cpudur
    nf      = min(a.nf, b.nf)
    njobs   = a.njobs + b.njobs
    return Record(mintime, maxtime, walldur, cpudur, nf, njobs)

Record.__add__ = record_adder

site_map = {
    'Crane':     'Nebraska',
    'Sandhills': 'Nebraska',
    'Tusker':    'Nebraska'
}

# Map a site + vo to a new site name
# Added by Derek to support LHCb's usage of the shared MIT CMS site
site_vo_map = {
    ('MIT_CMS', 'lhcb'): 'MIT_LHCb'
}

def add_record(recs, vo, site, cores, dn, bkt):
    if site in site_map:
        site = site_map[site]

    if (site, vo) in site_vo_map:
        site = site_vo_map[(site, vo)]

    rk  = RecordKey(vo, site, cores, dn)
    rec = bkt_record(bkt, site)

    recs[rk] += rec

def print_header(output_file = sys.stdout):
    print(fixed_header, file=output_file)

def print_rk_recr(year, month, rk, rec, output_file=sys.stdout):

    if rk.dn == "N/A":
        dn = "generic %s user" % rk.vo
    else:
        dn = rk.dn

    # With no hs23 portion, the submit host is just "hepspec-hosts"
    # With hs23 portion, it's both "hepspec-hosts" and "hepscore-hosts"
    submit_hosts = ["hepspec-hosts"]
    # Check the site name for the HS23 portion
    hs23_portion = get_hs23_portion(rk.site)
    if not isclose(hs23_portion, 0.0):
        submit_hosts.append("hepscore-hosts")
        
    # Quick lambda to write the lines
    write = lambda *line: print(*line, file=output_file)

    for submit_host in range(len(submit_hosts)):
        # Index 0 is hepspec-hosts, index 1 is hepscore-hosts
        # Do some clever math to get the portion

        if submit_host == 0:
            portion = 1.0 - hs23_portion
            metric_name = "hepspec"
        elif submit_host == 1:
            portion = hs23_portion
            metric_name = "HEPscore23"
        else:
            raise ValueError(f"Invalid submit_host: {submit_host}")
        
        write("Site:",                   rk.site)
        write("SubmitHost:",             submit_hosts[submit_host])
        write("VO:",                     rk.vo)
        write("EarliestEndTime:",        rec.mintime)
        write("LatestEndTime:",          rec.maxtime + 60*60*24 - 1)
        write("Month:",                  "%02d" % month)
        write("Year:",                   year)
        write("Infrastructure:",         fixed_infrastructure)
        write("GlobalUserName:",         dn)
        write("Processors:",             rk.cores)
        write("NodeCount:",              fixed_nodecount)
        write("WallDuration:",           int(rec.walldur * portion))
        write("CpuDuration:",            int(rec.cpudur * portion))
        write("NormalisedWallDuration:", "{" + metric_name + ": " + str(int(rec.walldur * rec.nf * portion)) + "}")
        write("NormalisedCpuDuration:",  "{" + metric_name + ": " + str(int(rec.cpudur  * rec.nf * portion)) + "}")
        write("NumberOfJobs:",           int(rec.njobs * portion))
        write(fixed_separator)

def bkt_key_lower(bkt):
    return bkt.key.lower()

def sorted_buckets(agg, key=operator.attrgetter('key')):
    return sorted(agg.buckets, key=key)

def auto_year_month():
    today = datetime.datetime.today()
    if today.day <= 3:
        onemonth = dateutil.relativedelta.relativedelta(months=1)
        lastmonth = today - onemonth
        return lastmonth.year, lastmonth.month
    else:
        return today.year, today.month

def main():
    if len(sys.argv[1:]) == 0:
        year,month = auto_year_month()
    else:
        try:
            year,month = map(int, sys.argv[1:])
        except:
            print("usage: %s [YEAR MONTH]" % os.path.basename(__file__), file=sys.stderr)
            sys.exit(0)

    outfile_name = "%02d_%d.apel" % (month, year)
    outfile = open(outfile_name, "w")

    resp = gracc_query_apel(year, month)
    aggs = resp.aggregations

    recs = autodict()

    print_header(outfile)
    for cores_bkt in sorted_buckets(aggs.Cores):
        cores = cores_bkt.key
        for vo_bkt in sorted_buckets(cores_bkt.VO):
            vo = vo_bkt.key
            for dn_bkt in sorted_buckets(vo_bkt.DN):
                dn = dn_bkt.key
                for site_bkt in sorted_buckets(dn_bkt.Site):
                    site = site_bkt.key
                    if site == MISSING:
                        for sitename_bkt in sorted_buckets(site_bkt.SiteName):
                            sitename = sitename_bkt.key
                            add_record(recs, vo, sitename, cores, dn,
                                       sitename_bkt)
                    else:
                        add_record(recs, vo, site, cores, dn, site_bkt)

    for rk,rec in sorted(recs.items()):
        print_rk_recr(year, month, rk, rec, outfile)

    print("wrote: %s" % outfile_name)

if __name__ == '__main__':
    main()

