"""GraphQL documents.

The exact document the Alexa app sends is not in ``ref/`` -- only the field
shapes are -- so this is reconstructed from ``ref/00`` §3, ``ref/04`` §1.2.2 and
``ref/06``. Two consequences:

* The ``properties`` union is spelled with explicit inline fragments covering
  every property type the target devices can return. If the server rejects an
  unknown fragment the query fails wholesale, so keep this list to types the
  spec actually attests.
* Field spellings are unsettled between the plugins' document and the live
  write-ups (``threads/002`` Q5). Both spellings are requested via aliases where
  they can coexist, and :mod:`.models` accepts either.
"""

from __future__ import annotations

from typing import Any

# One bulk query returns discovery *and* live state for every endpoint: ~550 ms
# for 73 endpoints, against ~22 s to poll them individually (ref/09). Asking for
# `configuration` alongside `properties` means capability and state arrive
# together, with no second round trip for bounds.
ENDPOINTS_QUERY = """
query Endpoints {
  endpoints {
    items {
      id
      friendlyName
      displayCategories { primary { value } }
      enablement
      model { value { text } }
      manufacturer { value { text } }
      features {
        name
        instance
        operations { name }
        properties {
          __typename
          ... on Power { powerStateValue }
          ... on ToggleState { toggleStateValue }
          ... on Mode { modeValue { value } }
          ... on RangeValue { rangeValue { value } }
          ... on TemperatureSensor { value { value scale } }
          ... on Setpoint { value { value scale } }
          ... on ThermostatMode { thermostatModeValue }
        }
        configuration {
          __typename
          ... on ModeConfiguration {
            order
            friendlyName { value { text } }
            modeOptions { value modeResources { friendlyName { value { text } } } }
          }
          ... on RangeConfiguration {
            friendlyName { value { text } }
            unitOfMeasure { value { text } }
            supportedRange { minimumValue maximumValue precision }
            presets { rangeValue }
          }
          ... on ToggleConfiguration { friendlyName { value { text } } }
          ... on ThermostatConfiguration { supportedModes }
        }
      }
    }
  }
}
"""

# Writes. `instance` is the field all three reference implementations omit, and
# the reason none of them can drive a fan speed or a cover (ref/06, ref/07).
# `errors` is a sibling of the responses: HTTP 200 does not mean the command
# worked, and a batch can partially succeed (ref/00 §5).
SET_ENDPOINT_FEATURES_MUTATION = """
mutation SetEndpointFeatures($input: SetEndpointFeaturesInput!) {
  setEndpointFeatures(setEndpointFeaturesInput: $input) {
    featureControlResponses {
      endpointId
      featureName
      instance
      featureOperationName
      code
      responsePayload
    }
    errors { endpointId code }
  }
}
"""


def feature_control_request(
    endpoint_id: str,
    feature_name: str,
    operation: str,
    *,
    instance: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one element of ``featureControlRequests``.

    ``payload`` keys on the bare property name -- ``{"mode": "2"}``, confirmed by
    execution -- and is ``{}`` for ``turnOn``/``turnOff``. ``instance`` is
    omitted rather than sent as null for features that have none, such as
    ``power``.
    """
    request: dict[str, Any] = {
        "endpointId": endpoint_id,
        "featureName": feature_name,
        "featureOperationName": operation,
        "payload": payload if payload is not None else {},
    }
    if instance is not None:
        request["instance"] = instance
    return request
