from rest_framework.pagination import PageNumberPagination


class FlexiblePagination(PageNumberPagination):
    """
    Custom pagination class that allows clients to specify page_size via query parameter.
    """
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 10000
