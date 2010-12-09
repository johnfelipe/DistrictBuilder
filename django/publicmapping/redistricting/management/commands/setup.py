"""
Set up District Builder.

This management command will examine the main configuration file for 
correctness, import geographic levels, create spatial views, create 
geoserver layers, and construct a default plan.

This file is part of The Public Mapping Project
http://sourceforge.net/projects/publicmapping/

License:
    Copyright 2010 Micah Altman, Michael McDonald

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

Author: 
    Andrew Jennings, David Zwarg
"""

from decimal import Decimal
from django.contrib.gis.gdal import *
from django.contrib.gis.geos import *
from django.contrib.gis.db.models import Union 
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.conf import settings
from optparse import make_option
from os.path import exists
from lxml.etree import parse
from xml.dom import minidom
from redistricting.models import *
from redistricting.utils import *
import traceback, pprint, httplib, string, base64, json

class Command(BaseCommand):
    """
    Set up District Builder.
    """
    args = '<config>'
    help = 'Sets up District Builder based on the main XML configuration.'
    option_list = BaseCommand.option_list + (
        make_option('-c', '--config', dest="config",
            help="Use configuration file CONFIG", metavar="CONFIG"),
        make_option('-d', '--debug', dest="debug",
            help="Generate verbose debug output", default=False, 
            action='store_true'),
        make_option('-g', '--geolevel', dest="geolevels",
            action="append", help="Geolevels to import"),
        make_option('-v', '--views', dest="views", default=False,
            action="store_true", help="Create database views."),
        make_option('-G', '--geoserver', dest="geoserver",
            action="store_true", help="Create spatial layers in Geoserver.",
            default=False),
        make_option('-t', '--templates', dest="templates",
            action="store_true", help="Create system templates based on district index files.", default=False),
    )


    def handle(self, *args, **options):
        """
        Perform the command. 
        """
        if options.get('config') is None:
            print """
ERROR:

    This management command requires the -c or --config option. This option
    specifies the main configuration file.
"""
            return

        verbose = options.get('debug')

        try:
            config = parse( options.get('config') )
        except Exception, ex:
            print """
ERROR:

The configuration file specified could not be parsed. Please check the
contents of the file and try again.
"""
            if verbose:
                print "The following traceback may provide more information:"
                print traceback.format_exc()
            return

        #
        # config is now a XSD validated and id-ref checked configuration
        #

        self.import_prereq(config, verbose)

        optlevels = options.get("geolevels")
        if not optlevels is None:
            # Begin the import process
            geolevels = config.xpath('/DistrictBuilder/GeoLevels/GeoLevel')

            for i,geolevel in enumerate(geolevels):
                importme = len(optlevels) == 0
                importme = importme or (i in optlevels)
                if importme:
                    self.import_geolevel(config, geolevel, verbose)

        if options.get("views"):
            # Create views based on the subjects and geolevels
            self.create_views(verbose)

        if options.get("geoserver"):
            extent = Geounit.objects.all().extent()
            self.configure_geoserver(config, extent, verbose)

        if options.get("templates"):
            print "Creating templates..."
            self.create_template(config, verbose)

    def configure_geoserver(self, config, extent, verbose):
        """
        Create the workspace and layers in geoserver, based on the
        imported data.
        """

        # Get the workspace information
        mapconfig = config.xpath('//MapServer')[0]

        host = mapconfig.get('hostname')
        if host == '':
            host = 'localhost'
        namespace = mapconfig.get('ns')
        namespacehref = mapconfig.get('nshref')

        user_pass = '%s:%s' % (mapconfig.get('adminuser'), mapconfig.get('adminpass'))
        auth = 'Basic %s' % string.strip(base64.encodestring(user_pass))
        headers = {'Authorization': auth, 'Content-Type': 'application/xml', 'Accepts':'application/xml'}

        if not self.rest_config( 'POST', host, 
            '/geoserver/rest/namespaces',
            '<?xml version="1.0" encoding="UTF-8"?><namespace><prefix>%s</prefix><uri>%s</uri></namespace>' % (namespace, namespacehref),
            headers,
            'Could not create workspace in geoserver.'):
            return False

        if verbose:
            print "Created geoserver workspace and namespace."

        dbconfig = config.xpath('//Database')[0]
        dbconn = '<connectionParameters><host>%s</host><port>5432</port><database>%s</database><user>%s</user><passwd>%s</passwd><dbtype>postgis</dbtype><namespace>%s</namespace><schema>%s</schema></connectionParameters>' % (host, dbconfig.get('name'), dbconfig.get('user'), dbconfig.get('password'), namespacehref, dbconfig.get('user'))

        datastore = 'PostGIS'
        if not self.rest_config( 'POST', host,
            '/geoserver/rest/workspaces/%s/datastores' % namespace,
            '<?xml version="1.0" encoding="UTF-8"?><dataStore><name>%s</name>%s</dataStore>' % (datastore, dbconn),
            headers,
            'Could not add PostGIS data store to geoserver.'):
            return False

        if verbose:
            print "Created geoserver PostGIS data store."

        if not self.rest_config( 'POST', host,
            '/geoserver/rest/workspaces/%s/datastores/%s/featuretypes' % (namespace, datastore),
            '<?xml version="1.0" encoding="UTF-8"?><featureType><name>identify_geounit</name><title>identify_geounit</title><nativeBoundingBox><minx>%0.1f</minx><miny>%0.1f</miny><maxx>%0.1f</maxx><maxy>%0.1f</maxy></nativeBoundingBox></featureType>' % extent,
            headers,
            'Could not create "identify_geounit" layer.'):
            return False

        if verbose:
            print "Created 'identify_geounit' layer."

        if not self.rest_config( 'POST', host,
            '/geoserver/rest/workspaces/%s/datastores/%s/featuretypes' % (namespace, datastore),
            '<?xml version="1.0" encoding="UTF-8"?><featureType><name>simple_district</name><title>simple_district</title><nativeBoundingBox><minx>%0.1f</minx><miny>%0.1f</miny><maxx>%0.1f</maxx><maxy>%0.1f</maxy></nativeBoundingBox></featureType>' % extent,
            headers,
            'Could not create "simple_district" layer.'):
            return False

        if verbose:
            print "Created 'simple_district' layer."

        for geolevel in Geolevel.objects.all():
            if not self.rest_config( 'POST', host,
                '/geoserver/rest/workspaces/%s/datastores/%s/featuretypes' % (namespace, datastore),
                '<?xml version="1.0" encoding="UTF-8"?><featureType><name>simple_%s</name><title>simple_%s</title><nativeBoundingBox><minx>%0.1f</minx><miny>%0.1f</miny><maxx>%0.1f</maxx><maxy>%0.1f</maxy></nativeBoundingBox></featureType>' % (geolevel.name, geolevel.name, extent[0], extent[1], extent[2], extent[3]),
                headers,
                'Could not create picking layer "simple_%s".' % geolevel.name):
                return False

            if verbose:
                print "Created 'simple_%s' layer." % geolevel.name

            styledir = mapconfig.get('styles')

            firstsubj = True
            for subject in Subject.objects.all():
                if not self.rest_config( 'POST', host,
                    '/geoserver/rest/workspaces/%s/datastores/%s/featuretypes' % (namespace, datastore),
                    '<?xml version="1.0" encoding="UTF-8"?><featureType><name>demo_%s_%s</name><title>demo_%s_%s</title><nativeBoundingBox><minx>%0.1f</minx><miny>%0.1f</miny><maxx>%0.1f</maxx><maxy>%0.1f</maxy></nativeBoundingBox></featureType>' % (geolevel.name, subject.name, geolevel.name, subject.name, extent[0], extent[1], extent[2], extent[3]),
                    headers,
                    "Could not create demographic layer 'demo_%s_%s'" % (geolevel.name, subject.name)):
                    return False

                if verbose:
                    print "Created 'demo_%s_%s' layer." % (geolevel.name, subject.name)

                sld = self.get_style_contents( styledir, 
                    geolevel.name, 
                    subject.name)
                if sld is None:
                    return False

                if not self.rest_config( 'POST', host,
                    '/geoserver/rest/styles',
                    '<?xml version="1.0" encoding="UTF-8"?><style><name>%s_%s</name><filename>%s_%s.sld</filename></style>' % (geolevel.name, subject.name, geolevel.name, subject.name),
                    headers,
                    "Could not create style '%s_%s'." % (geolevel.name, subject.name)):
                    return False

                if verbose:
                    print "Created '%s_%s' style." % (geolevel.name, subject.name)

                headers['Content-Type'] = 'application/vnd.ogc.sld+xml'
                if not self.rest_config( 'PUT', host,
                    '/geoserver/rest/styles/%s_%s' % (geolevel.name, subject.name),
                    sld,
                    headers,
                    "Could not upload style file '%s_%s.sld'" % (geolevel.name, subject.name)):
                    return False

                if verbose:
                    print "Uploaded '%s_%s.sld' file." % (geolevel.name, subject.name)

                headers['Content-Type'] = 'application/xml'
                if not self.rest_config( 'PUT', host,
                    '/geoserver/rest/layers/%s:demo_%s_%s' % (namespace, geolevel.name, subject.name),
                    '<?xml version="1.0" encoding="UTF-8"?><layer><defaultStyle><name>%s_%s</name></defaultStyle><enabled>true</enabled></layer>' % (geolevel.name, subject.name),
                    headers,
                    "Could not assign style '%s_%s' to layer 'demo_%s_%s'." % (geolevel.name, subject.name, geolevel.name, subject.name)):
                    return False

                if verbose:
                    print "Assigned style '%s_%s' to layer 'demo_%s_%s'." % (geolevel.name, subject.name, geolevel.name, subject.name)


                if firstsubj:
                    firstsubj = False
                    #
                    # Create NONE demographic layer, based on first subject
                    #
                    if not self.rest_config( 'POST', host,
                        '/geoserver/rest/workspaces/%s/datastores/%s/featuretypes' % (namespace, datastore),
                        '<?xml version="1.0" encoding="UTF-8"?><featureType><name>demo_%s</name><title>demo_%s</title><nativeName>demo_%s_%s</nativeName><nativeBoundingBox><minx>%0.1f</minx><miny>%0.1f</miny><maxx>%0.1f</maxx><maxy>%0.1f</maxy></nativeBoundingBox></featureType>' % (geolevel.name, geolevel.name, geolevel.name, subject.name, extent[0], extent[1], extent[2], extent[3]),
                        headers,
                        "Could not create demographic layer 'demo_%s'" % geolevel.name):
                        return False

                    if verbose:
                        print "Created 'demo_%s' layer." % geolevel.name

                    sld = self.get_style_contents( styledir, 
                        geolevel.name, 
                        'none' )
                    if sld is None:
                        return False

                    if not self.rest_config( 'POST', host,
                        '/geoserver/rest/styles',
                        '<?xml version="1.0" encoding="UTF-8"?><style><name>%s_none</name><filename>%s_none.sld</filename></style>' % (geolevel.name, geolevel.name),
                        headers,
                        "Could not create style '%s_none'." % geolevel.name):
                        return False

                    if verbose:
                        print "Created style '%s_none'." % geolevel.name

                    headers['Content-Type'] = 'application/vnd.ogc.sld+xml'
                    if not self.rest_config( 'PUT', host,
                        '/geoserver/rest/styles/%s_none' % geolevel.name,
                        sld,
                        headers,
                        "Could not upload style file '%s_none.sld'" % geolevel.name):
                        return False

                    if verbose:
                        print "Uploaded '%s_none.sld' file." % geolevel.name

                    headers['Content-Type'] = 'application/xml'
                    if not self.rest_config( 'PUT', host,
                        '/geoserver/rest/layers/%s:demo_%s' % (namespace, geolevel.name),
                        '<?xml version="1.0" encoding="UTF-8"?><layer><defaultStyle><name>%s_none</name></defaultStyle><enabled>true</enabled></layer>' % geolevel.name,
                        headers,
                        "Could not assign style '%s_none' to layer 'demo_%s'." % (geolevel.name, geolevel.name)):
                        return False

                    if verbose:
                        print "Assigned style '%s_none' to layer 'demo_%s'." % (geolevel.name, geolevel.name)

                    #
                    # Create boundary layer, based on geographic boundaries
                    #
                    if not self.rest_config( 'POST', host,
                        '/geoserver/rest/workspaces/%s/datastores/%s/featuretypes' % (namespace, datastore),
                        '<?xml version="1.0" encoding="UTF-8"?><featureType><name>%s_boundaries</name><title>%s_boundaries</title><nativeName>demo_%s_%s</nativeName><nativeBoundingBox><minx>%0.1f</minx><miny>%0.1f</miny><maxx>%0.1f</maxx><maxy>%0.1f</maxy></nativeBoundingBox></featureType>' % (geolevel.name, geolevel.name, geolevel.name, subject.name, extent[0], extent[1], extent[2], extent[3]),
                        headers,
                        "Could not create boundary layer '%s_boundaries'" % geolevel.name):
                        return False

                    if verbose:
                        print "Created '%s_boundaries' layer." % geolevel.name

                    sld = self.get_style_contents( styledir, 
                        geolevel.name, 
                        'boundaries' )
                    if sld is None:
                        return False

                    if not self.rest_config( 'POST', host,
                        '/geoserver/rest/styles',
                        '<?xml version="1.0" encoding="UTF-8"?><style><name>%s_boundaries</name><filename>%s_boundaries.sld</filename></style>' % (geolevel.name, geolevel.name),
                        headers,
                        "Could not create style '%s_boundaries'." % geolevel.name):
                        return False

                    if verbose:
                        print "Created style '%s_boundaries'." % geolevel.name

                    headers['Content-Type'] = 'application/vnd.ogc.sld+xml'
                    if not self.rest_config( 'PUT', host,
                        '/geoserver/rest/styles/%s_boundaries' % geolevel.name,
                        sld,
                        headers,
                        "Could not upload style file '%s_boundaries.sld'" % geolevel.name):
                        return False

                    if verbose:
                        print "Uploaded '%s_boundaries.sld' file." % geolevel.name

                    headers['Content-Type'] = 'application/xml'
                    if not self.rest_config( 'PUT', host,
                        '/geoserver/rest/layers/%s:%s_boundaries' % (namespace, geolevel.name),
                        '<?xml version="1.0" encoding="UTF-8"?><layer><defaultStyle><name>%s_boundaries</name></defaultStyle><enabled>true</enabled></layer>' % geolevel.name,
                        headers,
                        "Could not assign style '%s_boundaries' to layer '%s_boundaries'." % (geolevel.name, geolevel.name)):
                        return False

                    if verbose:
                        print "Assigned style '%s_boundaries' to layer '%s_boundaries'." % (geolevel.name, geolevel.name)


        return True

    def get_style_contents(self, styledir, geolevel, subject):
        path = '%s/%s_%s.sld' % (styledir, geolevel, subject) 
        try:
            stylefile = open(path)
            sld = stylefile.read()
            stylefile.close()

            return sld
        except:
            print """
ERROR:

        The style file %s colud not be loaded. Please confirm that the
        style files are named according to the "geolevel_subject.sld"
        convention, and try again.
""" % path
            return None

    def rest_config(self, method, host, url, data, headers, msg):
        try:
            conn = httplib.HTTPConnection(host, 8080)
            conn.request(method, url, data, headers)
            rsp = conn.getresponse()
            rsp.read() # and discard
            conn.close()
            if rsp.status != 201 and rsp.status != 200:
                print """
ERROR:

        Could not configure geoserver: 

        %s 

        Please check the configuration settings, and try again.
""" % msg
                print "HTTP Status: %d" % rsp.status
                return False
        except Exception, ex:
            print """
ERROR:

        Exception thrown while configuring geoserver.
"""
            return False

        return True

    @transaction.commit_manually
    def create_views(self, verbose):
        """
        Create specialized views for GIS and mapping layers.

        This creates views in the database that are used to map the features
        at different geographic levels, and for different choropleth map
        visualizations. All parameters for creating the views are saved
        in the database at this point.
        """
        cursor = connection.cursor()
        
        sql = "CREATE OR REPLACE VIEW simple_district AS SELECT rd.id, rd.district_id, rd.name, rd.version, rd.plan_id, rc.subject_id, rc.number, rd.simple AS geom FROM redistricting_district rd JOIN redistricting_computedcharacteristic rc ON rd.id = rc.district_id WHERE rd.version = (( SELECT max(redistricting_district.version) AS max FROM redistricting_district WHERE redistricting_district.district_id = rd.district_id));"
        cursor.execute(sql)
        transaction.commit()
        if verbose:
            print 'Created simple_district view ...'
        
        sql = "CREATE OR REPLACE VIEW identify_geounit AS SELECT rg.id, rg.name, rg.geolevel_id, rg.geom, rc.number, rc.percentage, rc.subject_id FROM redistricting_geounit rg JOIN redistricting_characteristic rc ON rg.id = rc.geounit_id;"
        cursor.execute(sql)
        transaction.commit()
        if verbose:
            print 'Created identify_geounit view ...'

        for geolevel in Geolevel.objects.all():
            sql = "CREATE OR REPLACE VIEW simple_%s AS SELECT id, name, geolevel_id, simple as geom FROM redistricting_geounit WHERE geolevel_id = %d;" % (geolevel.name, geolevel.id,)
            cursor.execute(sql)
            transaction.commit()
            if verbose:
                print 'Created simple_%s view ...' % geolevel.name
            
            for subject in Subject.objects.all():
                sql = "CREATE OR REPLACE VIEW demo_%s_%s AS SELECT rg.id, rg.name, rg.geolevel_id, rg.geom, rc.number, rc.percentage FROM redistricting_geounit rg JOIN redistricting_characteristic rc ON rg.id = rc.geounit_id WHERE rc.subject_id = %d AND rg.geolevel_id = %d;" % \
                    (geolevel.name, subject.name, 
                     subject.id, geolevel.id,)
                cursor.execute(sql)
                transaction.commit()
                if verbose:
                    print 'Created demo_%s_%s view ...' % \
                        (geolevel.name, subject.name)

    def import_geolevel(self, config, geolevel, verbose):
        """
        Import the geography at a geolevel.

        Parameters:
            config - The configuration dict of the geolevel
            geolevel - The geolevel node in the configuration
        """
        gconfig = {
            'shapepath': geolevel.get('shapefile'),
            'geolevel': geolevel.get('name'),
            'name_field': geolevel.get('namefield'),
            'supplemental_id_field': geolevel.get('supplementfield'),
            'subject_fields': []
        }

        trefs = geolevel.xpath('LegislativeBodies/LegislativeBody/LegislativeTargets/LegislativeTarget')
        for tref in trefs:
            tconfig = config.xpath('//Target[@id="%s"]' % tref.get('ref'))[0]

            sconfig = config.xpath('//Subject[@id="%s"]' % tconfig.get('subjectref'))[0]
            if 'aliasfor' in sconfig.attrib:
                salconfig = config.xpath('//Subject[@id="%s"]' % sconfig.get('aliasfor'))[0]
                sconfig.append(salconfig)
            gconfig['subject_fields'].append( sconfig )

        self.import_shape(gconfig, verbose)


    def import_prereq(self, config, verbose):
        """
        Import the required support data prior to importing.

        Import the LegislativeBody, Subject, Geolevel, and associated
        relationships prior to loading all the geounits.
        """

        # Import legislative bodies first.
        bodies = config.xpath('//LegislativeBody[@id]')
        for body in bodies:
            obj, created = LegislativeBody.objects.get_or_create(
                name=body.get('name'), 
                member=body.get('member'), 
                max_districts=body.get('maxdistricts'))
            if verbose:
                if created:
                    print 'Created LegislativeBody "%s"' % body.get('name')
                else:
                    print 'LegislativeBody "%s" already exists' % body.get('name')


        # Import subjects second
        subjs = config.xpath('//Subject[@id]')
        for subj in subjs:
            if 'aliasfor' in subj.attrib:
                continue
            obj, created = Subject.objects.get_or_create(
                name=subj.get('id'), 
                display=subj.get('name'), 
                short_display=subj.get('short_name'), 
                is_displayed=(subj.get('displayed')=='true'), 
                sort_key=subj.get('sortkey'))

            if verbose:
                if created:
                    print 'Created Subject "%s"' % subj.get('name')
                else:
                    print 'Subject "%s" already exists' % subj.get('name')

        # Import targets third
        targs = config.xpath('//Targets/Target')

        for targ in targs:
            # get subject
            subconfig = config.xpath('//Subject[@id="%s"]' % (targ.get('subjectref')))[0]
            if not subconfig.get('aliasfor') is None:
                # dereference any subject alias
                subconfig = config.xpath('//Subject[@id="%s"]' % (subconfig.get('aliasfor')))[0]
            subject = Subject.objects.filter(name=subconfig.get('id'))[0]

            obj, created = Target.objects.get_or_create(
                subject=subject,
                value=targ.get('value'),
                range1=targ.get('range1'),
                range2=targ.get('range2'))

            if verbose:
                if created:
                    print 'Created Target "%s"' % obj
                else:
                    print 'Target "%s" already exists' % obj
            
        # Import geolevels fourth
        # Note that geolevels may be added in any order, but the geounits
        # themselves need to be imported top-down (smallest area to biggest)
        geolevels = config.xpath('//GeoLevels/GeoLevel')
        for geolevel in geolevels:
            glvl,created = Geolevel.objects.get_or_create(name=geolevel.get('name'),min_zoom=geolevel.get('min_zoom'),sort_key=geolevel.get('sort_key'))

            if verbose:
                if created:
                    print 'Created GeoLevel "%s"' % glvl.name
                else:
                    print 'GeoLevel "%s" already exists' % glvl.name

            # Map the imported geolevel to a legislative body
            lbodies = geolevel.xpath('LegislativeBodies/LegislativeBody')
            for lbody in lbodies:
                # de-reference
                lbconfig = config.xpath('//LegislativeBody[@id="%s"]' % lbody.get('ref'))[0]
                legislative_body = LegislativeBody.objects.get(name=lbconfig.get('name'))
                
                # Add a mapping for the targets in this GL/LB combo.
                targs = lbody.xpath('LegislativeTargets/LegislativeTarget')
                for targ in targs:
                    tconfig = config.xpath('//Target[@id="%s"]' % targ.get('ref'))[0]
                    sconfig = config.xpath('//Subject[@id="%s"]' % tconfig.get('subjectref'))[0]
                    if not sconfig.get('aliasfor') is None:
                        # dereference any subject alias
                        sconfig = config.xpath('//Subject[@id="%s"]' % (sconfig.get('aliasfor')))[0]
                    subject = Subject.objects.get(name=sconfig.get('id'))

                    target = Target.objects.get(
                        subject=subject,
                        value=tconfig.get('value'),
                        range1=tconfig.get('range1'),
                        range2=tconfig.get('range2')) 

                    if not targ.get('default') is None:
                        obj, created = LegislativeDefaults.objects.get_or_create(legislative_body=legislative_body)
                        obj.target = target
                        obj.save()

                        if verbose:
                            if created:
                                print 'Set default target for LegislativeBody "%s"' % legislative_body.name
                            else:
                                print 'Changed default target for LegislativeBody "%s"' % legislative_body.name

                    pconfig = lbody.xpath('Parent')
                    if len(pconfig) == 0:
                        parent = None
                    else:
                        pconfig = config.xpath('//GeoLevel[@id="%s"]' % pconfig[0].get('ref'))[0]
                        plvl = Geolevel.objects.get(name=pconfig.get('name'))
                        parent = LegislativeLevel.objects.get(
                            legislative_body=legislative_body, 
                            geolevel=plvl, 
                            target=target)

                    obj, created = LegislativeLevel.objects.get_or_create(
                        legislative_body=legislative_body, 
                        geolevel=glvl, 
                        target=target, 
                        parent=parent)

                    if verbose:
                        if created:
                            print 'Created LegislativeBody/GeoLevel mapping "%s/%s"' % (legislative_body.name, glvl.name)
                        else:
                            print 'LegislativeBody/GeoLevel mapping "%s/%s" already exists' % (legislative_body.name, glvl.name)

        # Create an anonymous user
        anon,created = User.objects.get_or_create(username='anonymous')
        if not created:
            anon.set_password('anonymous')
            anon.save()

        return True

    def import_shape(self,config,verbose):
        """
        Import a shapefile, based on a config.

        Parameters:
            config -- A dictionary with 'shapepath', 'geolevel', 'name_field', and 'subject_fields' keys.
        """
        ds = DataSource(config['shapepath'])

        if verbose:
            print 'Importing from ', ds

        lyr = ds[0]
        if verbose:
            print '%d objects in shapefile' % len(lyr)

        level = Geolevel.objects.get(name=config['geolevel'])
        supplemental_id_field = config['supplemental_id_field']

        # Create the subjects we need
        subject_objects = {}
        for sconfig in config['subject_fields']:
            # don't recreate any subjects that already exist
            # (in another geolevel, for instance)
            foundalias = False
            for elem in sconfig.getchildren():
                if elem.tag == 'Subject':
                    foundalias = True
                    sub = Subject.objects.get(name=elem.get('id'))
            if not foundalias:
                sub = Subject.objects.get(name=sconfig.get('id'))
            subject_objects[sconfig.get('field')] = sub

        progress = 0.0
        if verbose:
            sys.stdout.write('0% .. ')
            sys.stdout.flush()
        for i,feat in enumerate(lyr):
            if (float(i) / len(lyr)) > (progress + 0.1):
                progress += 0.1
                if verbose:
                    sys.stdout.write('%2.0f%% .. ' % (progress * 100))
                    sys.stdout.flush()

            try :
                # Store the geos geometry
                geos = feat.geom.geos
                # Coerce the geometry into a MultiPolygon
                if geos.geom_type == 'MultiPolygon':
                    my_geom = geos
                elif geos.geom_type == 'Polygon':
                    my_geom = MultiPolygon(geos)
                simple = my_geom.simplify(tolerance=settings.SIMPLE_TOLERANCE,preserve_topology=True)
                if simple.geom_type != 'MultiPolygon':
                    simple = MultiPolygon(simple)
                center = my_geom.centroid

                geos = None

                # Ensure the centroid is within the geometry
                if not center.within(my_geom):
                    # Get the first polygon in the multipolygon
                    first_poly = my_geom[0]
                    # Get the extent of the first poly
                    first_poly_extent = first_poly.extent
                    min_x = first_poly_extent[0]
                    max_x = first_poly_extent[2]
                    # Create a line through the bbox and the poly center
                    my_y = first_poly.centroid.y
                    centerline = LineString( (min_x, my_y), (max_x, my_y))
                    # Get the intersection of that line and the poly
                    intersection = centerline.intersection(first_poly)
                    if type(intersection) is MultiLineString:
                        intersection = intersection[0]
                    # the center of that line is my within-the-poly centroid.
                    center = intersection.centroid
                    first_poly = first_poly_extent = min_x = max_x = my_y = centerline = intersection = None

                if verbose:
                    if not my_geom.simple:
                        print 'Geometry %d is not simple.\n' % feat.fid
                    if not my_geom.valid:
                        print 'Geometry %d is not valid.\n' % feat.fid
                    if not simple.simple:
                        print 'Simplified Geometry %d is not simple.\n' % feat.fid
                    if not simple.valid:
                        print 'Simplified Geometry %d is not valid.\n' % feat.fid

                g = Geounit(geom = my_geom, name = feat.get(config['name_field']), geolevel = level, simple = simple, center = center)
                if supplemental_id_field:
                    g.supplemental_id = feat.get(supplemental_id_field)
                g.save()
                my_geom = simple = center = None
            except:
                print 'Failed to import geometry for feature %d' % feat.fid
                if verbose:
                    traceback.print_exc()
                    print ''
                continue

            for attr, obj in subject_objects.iteritems():
                value = Decimal(str(feat.get(attr))).quantize(Decimal('000000.0000', 'ROUND_DOWN'))
                try:
                    c = Characteristic(subject=obj, number=value, geounit=g)
                    c.save()
                except:
                    c = Characteristic(subject=obj, number='0.0', geounit=g)
                    c.save()
                    print 'Failed to set value "%s" to %d in feature "%s"' % (attr, feat.get(attr), feat.get(config['name_field']),)
                    if verbose:
                        traceback.print_exc()
                        print ''
                c = value = None
            g = feat = None

        ds = None

        if verbose:
            sys.stdout.write('100%\n')

    def create_template(self, config, verbose):
        """
        Create the templates that are defined in the configuration file.
        In addition to creating templates explicitly specified, this
        will also create a blank template for each LegislativeBody.

        Parameters:
            config - The XML configuration.
            verbose - A flag for outputting messages during the process.
        """
        templates = config.xpath('/DistrictBuilder/Templates/Template')
        for template in templates:
            templateplan = Plan.objects.filter(name=template.get('name'))
            if len(templateplan) > 0:
                if verbose:
                    print "Plan '%s' exists, skipping." % template.get('name')
                continue

            lbconfig = config.xpath('//LegislativeBody[@id="%s"]' % template.xpath('LegislativeBody')[0].get('ref'))[0]
            legislative_body = LegislativeBody.objects.filter(name=lbconfig.get('name'))
            if len(legislative_body) == 0:
                if verbose:
                    print "LegislativeBody '%s' does not exist, skipping." % lbconfig.get('ref')
                continue

            fconfig = template.xpath('Blockfile')[0]
            path = fconfig.get('path')

            admin = User.objects.get(username=settings.ADMINS[0][0])
            DistrictIndexFile.index2plan( template.get('name'), legislative_body[0].id, path, owner=admin, template=True, purge=False, email=None)

            if verbose:
                print 'Created template plan "%s"' % template.get('name')

        lbodies = config.xpath('//LegislativeBody[@id]')
        for lbody in lbodies:
            owner = User.objects.get(is_staff=True)
            legislative_body = LegislativeBody.objects.get(name=lbody.get('name'))
            plan,created = Plan.objects.get_or_create(name='Blank',legislative_body=legislative_body,owner=owner,is_template=True)

            if verbose:
                if created:
                    print 'Created Plan named "Blank" for LegislativeBody "%s"' % legislative_body.name
                else:
                    print 'Plan named "Blank" for LegislativeBody "%s" already exists' % legislative_body.name
