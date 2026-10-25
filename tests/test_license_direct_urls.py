"""Synthetic uv-export input; no downloaded fixtures or native artifacts."""
from scripts._licenses_lib import runtime_closure_from_uv_export


def test_direct_wheels_remain_in_license_inventory():
    requirements = '''# generated requirements
sherpa-onnx @ https://example.invalid/binding.whl ; python_version == "3.13"
sherpa-onnx-core @ https://example.invalid/core.whl
some-library[extra] @ https://example.invalid/library.whl
numpy==2.3.4 ; python_version >= "3.12"
https://example.invalid/unnamed.whl
    --hash=sha256:abcdef
'''
    assert runtime_closure_from_uv_export(requirements) == {
        'sherpa_onnx', 'sherpa_onnx_core', 'some_library', 'numpy',
    }
