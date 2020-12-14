import datetime
import json
import random

from django import forms
from django.core.exceptions import PermissionDenied
from django.db.models import Sum, Q, OrderBy, F
from django.core.serializers.json import DjangoJSONEncoder
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext as _, gettext_lazy
import django_filters
from django_filters.constants import EMPTY_VALUES
from wagtail.admin import messages
from wagtail.admin.action_menu import ActionMenuItem
from wagtail.admin.filters import DateRangePickerWidget, WagtailFilterSet
from wagtail.admin.views.reports import ReportView
from wagtail.core.models import Page, PAGE_MODEL_CLASSES, UserPagePermissionsProxy

from .models import AbTest
from .events import EVENT_TYPES


class CreateAbTestForm(forms.ModelForm):
    goal_event = forms.ChoiceField(choices=[])
    hypothesis = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['goal_event'].choices = [
            (slug, goal.name)
            for slug, goal in EVENT_TYPES.items()
        ]

    def save(self, page, treatment_revision, user):
        ab_test = super().save(commit=False)
        ab_test.page = page
        ab_test.treatment_revision = treatment_revision
        ab_test.created_by = user
        ab_test.save()
        return ab_test

    class Meta:
        model = AbTest
        fields = ['name', 'hypothesis', 'goal_event', 'goal_page', 'sample_size']


def add_ab_test_checks(request, page):
    # User must have permission to edit the page
    page_perms = page.permissions_for_user(request.user)
    if not page_perms.can_edit():
        raise PermissionDenied

    # User must have permission to add A/B tests
    if not request.user.has_perm('wagtail_ab_testing.add_abtest'):
        raise PermissionDenied

    # Page must not already be running an A/B test
    if AbTest.objects.get_current_for_page(page=page):
        messages.error(request, _("This page already has a running A/B test"))

        return redirect('wagtailadmin_pages:edit', page.id)

    # Page must be published and have a draft revision
    if not page.live or not page.has_unpublished_changes:
        messages.error(request, _("To run an A/B test on this page, it must be live with draft changes."))

        return redirect('wagtailadmin_pages:edit', page.id)


def add_compare(request, page_id):
    page = get_object_or_404(Page, id=page_id).specific

    # Run some checks
    response = add_ab_test_checks(request, page)
    if response:
        return response

    latest_revision_as_page = page.get_latest_revision().as_page_object()
    comparison = page.get_edit_handler().get_comparison()
    comparison = [comp(page, latest_revision_as_page) for comp in comparison]
    comparison = [comp for comp in comparison if comp.has_changed()]

    return render(request, 'wagtail_ab_testing/add_compare.html', {
        'page': page,
        'latest_revision_as_page': latest_revision_as_page,
        'comparison': comparison,
        'differences': any(comp.has_changed() for comp in comparison),
    })


def add_form(request, page_id):
    page = get_object_or_404(Page, id=page_id)

    # Run some checks
    response = add_ab_test_checks(request, page)
    if response:
        return response

    if request.method == 'POST':
        form = CreateAbTestForm(request.POST)

        if form.is_valid():
            ab_test = form.save(page, page.get_latest_revision(), request.user)

            if 'start' in request.POST:
                ab_test.start()

            return redirect('wagtailadmin_pages:edit', page.id)
    else:
        form = CreateAbTestForm()

    return render(request, 'wagtail_ab_testing/add_form.html', {
        'page': page,
        'form': form,
        'goal_selector_props': json.dumps({
            'goalTypesByPageType': {
                f'{page_type._meta.app_label}.{page_type._meta.model_name}': [
                    {
                        'slug': slug,
                        'name': event_type.name,
                    }
                    for slug, event_type in EVENT_TYPES.items()
                    if event_type.can_be_triggered_on_page_type(page_type)
                ]
                for page_type in PAGE_MODEL_CLASSES
            }
        }, cls=DjangoJSONEncoder)
    })


class StartAbTestMenuItem(ActionMenuItem):
    name = 'action-start-ab-test'
    label = _("Start A/B test")

    def is_shown(self, request, context):
        return context['ab_test'].status == AbTest.Status.DRAFT


class RestartAbTestMenuItem(ActionMenuItem):
    name = 'action-restart-ab-test'
    label = _("Restart A/B test")

    def is_shown(self, request, context):
        return context['ab_test'].status == AbTest.Status.PAUSED


class EndAbTestMenuItem(ActionMenuItem):
    name = 'action-end-ab-test'
    label = _("End A/B test")

    def is_shown(self, request, context):
        return context['ab_test'].status in [AbTest.Status.DRAFT, AbTest.Status.RUNNING, AbTest.Status.PAUSED]


class PauseAbTestMenuItem(ActionMenuItem):
    name = 'action-pause-ab-test'
    label = _("Pause A/B test")

    def is_shown(self, request, context):
        return context['ab_test'].status == AbTest.Status.RUNNING


class AbTestActionMenu:
    template = 'wagtailadmin/pages/action_menu/menu.html'

    def __init__(self, request, **kwargs):
        self.request = request
        self.context = kwargs
        self.context['user_page_permissions'] = UserPagePermissionsProxy(self.request.user)

        self.menu_items = [
            StartAbTestMenuItem(order=0),
            RestartAbTestMenuItem(order=1),
            EndAbTestMenuItem(order=2),
            PauseAbTestMenuItem(order=3)
        ]

        self.menu_items = [
            menu_item
            for menu_item in self.menu_items
            if menu_item.is_shown(self.request, self.context)
        ]

        try:
            self.default_item = self.menu_items.pop(0)
        except IndexError:
            self.default_item = None

    def render_html(self):
        return render_to_string(self.template, {
            'default_menu_item': self.default_item.render_html(self.request, self.context),
            'show_menu': bool(self.menu_items),
            'rendered_menu_items': [
                menu_item.render_html(self.request, self.context)
                for menu_item in self.menu_items
            ],
        }, request=self.request)

    @cached_property
    def media(self):
        media = forms.Media()
        for item in self.menu_items:
            media += item.media
        return media


def progress(request, page, ab_test):
    if request.method == 'POST':
        if 'action-start-ab-test' in request.POST or 'action-restart-ab-test' in request.POST:
            if ab_test.status in [AbTest.Status.DRAFT, AbTest.Status.PAUSED]:
                ab_test.start()

                messages.success(request, _("A/B test started!"))
            else:
                messages.error(request, _("The A/B test must be in draft or paused in order to be started."))

        elif 'action-end-ab-test' in request.POST:
            if ab_test.status in [AbTest.Status.DRAFT, AbTest.Status.RUNNING, AbTest.Status.PAUSED]:
                ab_test.finish(cancel=True)
            else:
                messages.error(request, _("The A/B test has already ended."))

        elif 'action-pause-ab-test' in request.POST:
            if ab_test.status == AbTest.Status.RUNNING:
                ab_test.pause()
            else:
                messages.error(request, _("The A/B test cannot be paused because it is not running."))

        else:
            messages.error(request, _("Unknown action"))

        # Redirect back
        return redirect('wagtailadmin_pages:edit', page.id)

    # Fetch stats from database
    stats = ab_test.hourly_logs.aggregate(
        control_participants=Sum('participants', filter=Q(variant=AbTest.Variant.CONTROL)),
        control_conversions=Sum('conversions', filter=Q(variant=AbTest.Variant.CONTROL)),
        treatment_participants=Sum('participants', filter=Q(variant=AbTest.Variant.TREATMENT)),
        treatment_conversions=Sum('conversions', filter=Q(variant=AbTest.Variant.TREATMENT)),
    )
    control_participants = stats['control_participants'] or 0
    control_conversions = stats['control_conversions'] or 0
    treatment_participants = stats['treatment_participants'] or 0
    treatment_conversions = stats['treatment_conversions'] or 0

    current_sample_size = control_participants + treatment_participants

    estimated_completion_date = None
    if ab_test.status == AbTest.Status.RUNNING and current_sample_size:
        running_duration_days = ab_test.total_running_duration().days

        if running_duration_days > 0:
            participants_per_day = current_sample_size / ab_test.total_running_duration().days
            estimated_days_remaining = (ab_test.sample_size - current_sample_size) / participants_per_day
            estimated_completion_date = timezone.now().date() + datetime.timedelta(days=estimated_days_remaining)

    # Generate time series data for the chart
    time_series = []
    control = 0
    treatment = 0
    date = None
    for log in ab_test.hourly_logs.order_by('date', 'hour'):
        # Accumulate the conversions
        if log.variant == AbTest.Variant.CONTROL:
            control += log.conversions
        else:
            treatment += log.conversions

        while date is None or date < log.date:
            if date is None:
                # First record
                date = log.date
            else:
                # Move time forward to match log record
                date += datetime.timedelta(days=1)

            # Generate a log for this time
            time_series.append({
                'date': date,
                'control': control,
                'treatment': treatment,
            })

    return render(request, 'wagtail_ab_testing/progress.html', {
        'page': page,
        'ab_test': ab_test,
        'current_sample_size': current_sample_size,
        'current_sample_size_percent': int(current_sample_size / ab_test.sample_size * 100),
        'control_conversions': control_conversions,
        'control_participants': control_participants,
        'control_conversions_percent': int(control_conversions / control_participants * 100) if control_participants else 0,
        'treatment_conversions': treatment_conversions,
        'treatment_participants': treatment_participants,
        'treatment_conversions_percent': int(treatment_conversions / treatment_participants * 100) if treatment_participants else 0,
        'control_is_winner': ab_test.winning_variant == AbTest.Variant.CONTROL,
        'treatment_is_winner': ab_test.winning_variant == AbTest.Variant.TREATMENT,
        'estimated_completion_date': estimated_completion_date,
        'chart_data': json.dumps({
            'x': 'x',
            'columns': [
                ['x'] + [data_point['date'].isoformat() for data_point in time_series],
                [_("Control")] + [data_point['control'] for data_point in time_series],
                [_("Treatment")] + [data_point['treatment'] for data_point in time_series],
            ],
            'type': 'spline',
        }),
        'action_menu': AbTestActionMenu(request, view='edit', page=page, ab_test=ab_test),
    })


def compare_draft(request, page_id):
    page = get_object_or_404(Page, id=page_id).specific

    latest_revision_as_page = page.get_latest_revision().as_page_object()
    comparison = page.get_edit_handler().get_comparison()
    comparison = [comp(page, latest_revision_as_page) for comp in comparison]
    comparison = [comp for comp in comparison if comp.has_changed()]

    return render(request, 'wagtail_ab_testing/compare.html', {
        'page': page,
        'latest_revision_as_page': latest_revision_as_page,
        'comparison': comparison,
    })


# TEMPORARY
def add_test_participants(request, ab_test_id):
    ab_test = get_object_or_404(AbTest, id=ab_test_id)

    for i in range(int(ab_test.sample_size / 10)):
        ab_test.add_participant()

    return redirect('wagtailadmin_pages:edit', ab_test.page_id)


def add_test_conversions(request, ab_test_id, variant):
    ab_test = get_object_or_404(AbTest, id=ab_test_id)

    for i in range(int(ab_test.sample_size / 10)):
        ab_test.log_conversion(variant, time=timezone.now() - datetime.timedelta(days=random.randint(1, 20), hours=random.randint(0, 24)))

    return redirect('wagtailadmin_pages:edit', ab_test.page_id)


class SearchPageTitleFilter(django_filters.CharFilter):
    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs

        return qs.filter(page__title__icontains=value)


class AbTestingReportFilterSet(WagtailFilterSet):
    page = SearchPageTitleFilter()
    first_started_at = django_filters.DateFromToRangeFilter(label=gettext_lazy("Started at"), widget=DateRangePickerWidget)

    class Meta:
        model = AbTest
        fields = ['status', 'page', 'first_started_at']


class AbTestingReportView(ReportView):
    template_name = 'wagtail_ab_testing/report.html'
    title = gettext_lazy('A/B testing')
    header_icon = ''

    filterset_class = AbTestingReportFilterSet

    def get_queryset(self):
        return AbTest.objects.all().order_by(OrderBy(F('first_started_at'), descending=True, nulls_first=True))
