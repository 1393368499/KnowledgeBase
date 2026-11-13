"""DRF test client with response types for methods inherited from APIRequestFactory."""

from typing import TYPE_CHECKING, Any

from rest_framework.test import APIClient as DRFAPIClient

if TYPE_CHECKING:
    from django.http import HttpResponse

    class TestResponse(HttpResponse):
        data: Any

        def json(self, **kwargs: Any) -> Any: ...

    class APIClient(DRFAPIClient):
        # These methods dispatch through Django's Client and return responses.
        def get(self, *args: Any, **kwargs: Any) -> TestResponse: ...  # pyright: ignore[reportIncompatibleMethodOverride]
        def post(self, *args: Any, **kwargs: Any) -> TestResponse: ...  # pyright: ignore[reportIncompatibleMethodOverride]
        def put(self, *args: Any, **kwargs: Any) -> TestResponse: ...  # pyright: ignore[reportIncompatibleMethodOverride]
        def patch(self, *args: Any, **kwargs: Any) -> TestResponse: ...  # pyright: ignore[reportIncompatibleMethodOverride]
        def delete(self, *args: Any, **kwargs: Any) -> TestResponse: ...  # pyright: ignore[reportIncompatibleMethodOverride]
else:
    APIClient = DRFAPIClient
