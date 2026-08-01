"""
Filtres avances pour la consultation du journal d'audit.

Filtres standards (DjangoFilterBackend) :
  - user, action, model_name, institution
  - timestamp_after / timestamp_before (date range)

Filtres custom :
  - search : OR sur label / object_id / model_name / user.username
  - object_id : exact match (utilise par by-entity)
"""
import django_filters as df

from core.models import AuditLog


class AuditLogFilter(df.FilterSet):
    timestamp_after = df.IsoDateTimeFilter(
        field_name='timestamp', lookup_expr='gte',
    )
    timestamp_before = df.IsoDateTimeFilter(
        field_name='timestamp', lookup_expr='lte',
    )
    user = df.NumberFilter(field_name='user_id')
    institution = df.NumberFilter(field_name='institution_id')
    action = df.CharFilter(field_name='action')
    model_name = df.CharFilter(field_name='model_name')
    object_id = df.CharFilter(field_name='object_id')
    keep_forever = df.BooleanFilter(field_name='keep_forever')
    search = df.CharFilter(method='filter_search')

    class Meta:
        model = AuditLog
        fields = [
            'user', 'institution', 'action', 'model_name', 'object_id',
            'keep_forever', 'timestamp_after', 'timestamp_before', 'search',
        ]

    def filter_search(self, qs, name, value):
        if not value:
            return qs
        v = value.strip()
        from django.db.models import Q
        return qs.filter(
            Q(label__icontains=v)
            | Q(model_name__icontains=v)
            | Q(object_id__icontains=v)
            | Q(user__username__icontains=v)
            | Q(endpoint__icontains=v)
        )
