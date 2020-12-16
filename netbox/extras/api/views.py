from django.contrib.contenttypes.models import ContentType
from django.db.models.functions import Coalesce
from django.http import Http404
from django_rq.queues import get_connection
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.routers import APIRootView
from rest_framework.viewsets import ReadOnlyModelViewSet, ViewSet
from rq import Worker

from extras import filters
from extras.choices import JobResultStatusChoices
from extras.models import (
    ConfigContext, ExportTemplate, ImageAttachment, ObjectChange, JobResult, Tag, TaggedItem,
)
from extras.models import CustomField
from extras.reports import get_report, get_reports, run_report
from extras.scripts import get_script, get_scripts, run_script
from netbox.api.authentication import IsAuthenticatedOrLoginNotRequired
from netbox.api.metadata import ContentTypeMetadata
from netbox.api.views import ModelViewSet
from utilities.exceptions import RQWorkerNotRunningException
from utilities.utils import copy_safe_request, get_subquery
from . import serializers


class ExtrasRootView(APIRootView):
    """
    Extras API root view
    """
    def get_view_name(self):
        return 'Extras'


class ConfigContextQuerySetMixin:
    """
    Used by views that work with config context models (device and virtual machine).
    Provides a get_queryset() method which deals with adding the config context
    data annotation or not.
    """

    def get_queryset(self):
        """
        Build the proper queryset based on the request context

        If the `brief` query param equates to True or the `exclude` query param
        includes `config_context` as a value, return the base queryset.

        Else, return the queryset annotated with config context data
        """

        request = self.get_serializer_context()['request']
        if request.query_params.get('brief') or 'config_context' in request.query_params.get('exclude', []):
            return self.queryset
        return self.queryset.annotate_config_context_data()


#
# Custom fields
#

class CustomFieldViewSet(ModelViewSet):
    metadata_class = ContentTypeMetadata
    queryset = CustomField.objects.all()
    serializer_class = serializers.CustomFieldSerializer
    filterset_class = filters.CustomFieldFilterSet


class CustomFieldModelViewSet(ModelViewSet):
    """
    Include the applicable set of CustomFields in the ModelViewSet context.
    """

    def get_serializer_context(self):

        # Gather all custom fields for the model
        content_type = ContentType.objects.get_for_model(self.queryset.model)
        custom_fields = content_type.custom_fields.all()

        context = super().get_serializer_context()
        context.update({
            'custom_fields': custom_fields,
        })
        return context


#
# Export templates
#

class ExportTemplateViewSet(ModelViewSet):
    metadata_class = ContentTypeMetadata
    queryset = ExportTemplate.objects.all()
    serializer_class = serializers.ExportTemplateSerializer
    filterset_class = filters.ExportTemplateFilterSet


#
# Tags
#

class TagViewSet(ModelViewSet):
    queryset = Tag.objects.annotate(
        tagged_items=Coalesce(get_subquery(TaggedItem, 'tag'), 0)
    )
    serializer_class = serializers.TagSerializer
    filterset_class = filters.TagFilterSet


#
# Image attachments
#

class ImageAttachmentViewSet(ModelViewSet):
    metadata_class = ContentTypeMetadata
    queryset = ImageAttachment.objects.all()
    serializer_class = serializers.ImageAttachmentSerializer
    filterset_class = filters.ImageAttachmentFilterSet


#
# Config contexts
#

class ConfigContextViewSet(ModelViewSet):
    queryset = ConfigContext.objects.prefetch_related(
        'regions', 'sites', 'roles', 'platforms', 'tenant_groups', 'tenants',
    )
    serializer_class = serializers.ConfigContextSerializer
    filterset_class = filters.ConfigContextFilterSet


#
# Reports
#

class ReportViewSet(ViewSet):
    permission_classes = [IsAuthenticatedOrLoginNotRequired]
    _ignore_model_permissions = True
    exclude_from_schema = True
    lookup_value_regex = '[^/]+'  # Allow dots

    def _retrieve_report(self, pk):

        # Read the PK as "<module>.<report>"
        if '.' not in pk:
            raise Http404
        module_name, report_name = pk.split('.', 1)

        # Raise a 404 on an invalid Report module/name
        report = get_report(module_name, report_name)
        if report is None:
            raise Http404

        return report

    def list(self, request):
        """
        Compile all reports and their related results (if any). Result data is deferred in the list view.
        """
        report_list = []
        report_content_type = ContentType.objects.get(app_label='extras', model='report')
        results = {
            r.name: r
            for r in JobResult.objects.filter(
                obj_type=report_content_type,
                status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
            ).defer('data')
        }

        # Iterate through all available Reports.
        for module_name, reports in get_reports():
            for report in reports:

                # Attach the relevant JobResult (if any) to each Report.
                report.result = results.get(report.full_name, None)
                report_list.append(report)

        serializer = serializers.ReportSerializer(report_list, many=True, context={
            'request': request,
        })

        return Response(serializer.data)

    def retrieve(self, request, pk):
        """
        Retrieve a single Report identified as "<module>.<report>".
        """

        # Retrieve the Report and JobResult, if any.
        report = self._retrieve_report(pk)
        report_content_type = ContentType.objects.get(app_label='extras', model='report')
        report.result = JobResult.objects.filter(
            obj_type=report_content_type,
            name=report.full_name,
            status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
        ).first()

        serializer = serializers.ReportDetailSerializer(report, context={
            'request': request
        })

        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def run(self, request, pk):
        """
        Run a Report identified as "<module>.<script>" and return the pending JobResult as the result
        """
        # Check that the user has permission to run reports.
        if not request.user.has_perm('extras.run_script'):
            raise PermissionDenied("This user does not have permission to run reports.")

        # Check that at least one RQ worker is running
        if not Worker.count(get_connection('default')):
            raise RQWorkerNotRunningException()

        # Retrieve and run the Report. This will create a new JobResult.
        report = self._retrieve_report(pk)
        report_content_type = ContentType.objects.get(app_label='extras', model='report')
        job_result = JobResult.enqueue_job(
            run_report,
            report.full_name,
            report_content_type,
            request.user
        )
        report.result = job_result

        serializer = serializers.ReportDetailSerializer(report, context={'request': request})

        return Response(serializer.data)


#
# Scripts
#

class ScriptViewSet(ViewSet):
    permission_classes = [IsAuthenticatedOrLoginNotRequired]
    _ignore_model_permissions = True
    exclude_from_schema = True
    lookup_value_regex = '[^/]+'  # Allow dots

    def _get_script(self, pk):
        module_name, script_name = pk.split('.')
        script = get_script(module_name, script_name)
        if script is None:
            raise Http404
        return script

    def list(self, request):

        script_content_type = ContentType.objects.get(app_label='extras', model='script')
        results = {
            r.name: r
            for r in JobResult.objects.filter(
                obj_type=script_content_type,
                status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
            ).defer('data').order_by('created')
        }

        flat_list = []
        for script_list in get_scripts().values():
            flat_list.extend(script_list.values())

        # Attach JobResult objects to each script (if any)
        for script in flat_list:
            script.result = results.get(script.full_name, None)

        serializer = serializers.ScriptSerializer(flat_list, many=True, context={'request': request})

        return Response(serializer.data)

    def retrieve(self, request, pk):
        script = self._get_script(pk)
        script_content_type = ContentType.objects.get(app_label='extras', model='script')
        script.result = JobResult.objects.filter(
            obj_type=script_content_type,
            name=script.full_name,
            status__in=JobResultStatusChoices.TERMINAL_STATE_CHOICES
        ).first()
        serializer = serializers.ScriptDetailSerializer(script, context={'request': request})

        return Response(serializer.data)

    def post(self, request, pk):
        """
        Run a Script identified as "<module>.<script>" and return the pending JobResult as the result
        """
        script = self._get_script(pk)()
        input_serializer = serializers.ScriptInputSerializer(data=request.data)

        # Check that at least one RQ worker is running
        if not Worker.count(get_connection('default')):
            raise RQWorkerNotRunningException()

        if input_serializer.is_valid():
            data = input_serializer.data['data']
            commit = input_serializer.data['commit']

            script_content_type = ContentType.objects.get(app_label='extras', model='script')
            job_result = JobResult.enqueue_job(
                run_script,
                script.full_name,
                script_content_type,
                request.user,
                data=data,
                request=copy_safe_request(request),
                commit=commit
            )
            script.result = job_result
            serializer = serializers.ScriptDetailSerializer(script, context={'request': request})

            return Response(serializer.data)

        return Response(input_serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#
# Change logging
#

class ObjectChangeViewSet(ReadOnlyModelViewSet):
    """
    Retrieve a list of recent changes.
    """
    metadata_class = ContentTypeMetadata
    queryset = ObjectChange.objects.prefetch_related('user')
    serializer_class = serializers.ObjectChangeSerializer
    filterset_class = filters.ObjectChangeFilterSet


#
# Job Results
#

class JobResultViewSet(ReadOnlyModelViewSet):
    """
    Retrieve a list of job results
    """
    queryset = JobResult.objects.prefetch_related('user')
    serializer_class = serializers.JobResultSerializer
    filterset_class = filters.JobResultFilterSet


#
# ContentTypes
#

class ContentTypeViewSet(ReadOnlyModelViewSet):
    """
    Read-only list of ContentTypes. Limit results to ContentTypes pertinent to NetBox objects.
    """
    queryset = ContentType.objects.order_by('app_label', 'model')
    serializer_class = serializers.ContentTypeSerializer
    filterset_class = filters.ContentTypeFilterSet
