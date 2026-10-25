# Source and component licensing

Lanreal's original contributions to Whisperstream use **GPL-3.0-only**. The
upstream Chirp MIT grant remains available for upstream material. This project
does not offer an Apache dual license for Lanreal's contributions. See
[LICENSE](../LICENSE), [NOTICE](../NOTICE) and [authors](../AUTHORS.md).

You may use and modify GPL-covered software privately, including commercially.
Distributing covered derivatives or binaries requires GPL compliance, including
recipient rights and corresponding source. Ordinary GPL does not require public
release of private modifications or changes used only to provide a network
service. Neither the GPL nor contribution policy transfers your copyright to us.

## Component boundaries

Apache-2.0 components may be incorporated into a GPLv3 combined work without
erasing their own grant. A GPL-3.0-or-later component permits choosing version 3;
GPLv2-only is not compatible with this route. LGPL library requirements, MPL
file-level obligations and runtime exceptions need their own compliance review.
See [GPLv3](https://www.gnu.org/licenses/gpl-3.0.html),
[Apache compatibility](https://www.apache.org/licenses/GPL-compatibility) and
[Mozilla's guidance](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

| Component | Finding | Distribution consideration |
| --- | --- | --- |
| sherpa-onnx 1.13.8 source | Apache-2.0 | Compatible with GPLv3 |
| Pinned eSpeak-ng fork, `ed530aa113046142eb5115cf2fc9157854d0ffe1` | GPL-3.0-or-later, confirmed by project and source-file grants | Version 3 is available; this is the strong-copyleft dependency |
| eSpeak's bundled ucd-tools | GPL-3.0-or-later | Same version-3 route |
| Pinned piper-phonemize fork, `f3ff95afc03640bc1399e113e83361192a2fafb4` | MIT source, linked to eSpeak | Its own MIT license does not remove eSpeak's GPL obligations |
| Official Windows sherpa core wheel | eSpeak markers remain in the native DLL | The split into a core package has not removed GPL code |
| pynput 1.8.1 | LGPL-3.0-or-later in installed source headers | Library obligations apply; this is not ordinary GPL |
| pygame 2.6.1 / pygame-ce 2.5.7 | LGPL family; pygame 2.6.1 explicitly identifies LGPL 2.1 | Preserve library source/replacement obligations; does not by itself require the app source to be GPL |
| certifi and the inventoried MPL crates/tools | MPL-2.0 | Preserve file-level obligations; review secondary-license conditions when combining with GPL |
| GCC runtime bundled in NumPy 2.3.4's OpenBLAS DLL | GPL-3.0-or-later WITH GCC-exception-3.1 | Assess the runtime exception, not a bare GPL string |

Native pins are recorded in upstream's
[eSpeak CMake input](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/cmake/espeak-ng-for-piper.cmake)
and [piper CMake input](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/cmake/piper-phonemize.cmake).
The actual grants are in the pinned
[eSpeak README](https://github.com/csukuangfj/espeak-ng/blob/ed530aa113046142eb5115cf2fc9157854d0ffe1/README.md#license-information),
[compiled source header](https://github.com/csukuangfj/espeak-ng/blob/ed530aa113046142eb5115cf2fc9157854d0ffe1/src/libespeak-ng/synthesize.c),
[ucd-tools README](https://github.com/csukuangfj/espeak-ng/blob/ed530aa113046142eb5115cf2fc9157854d0ffe1/src/ucd-tools/README.md#license-information)
and [piper license](https://github.com/csukuangfj/piper-phonemize/blob/f3ff95afc03640bc1399e113e83361192a2fafb4/LICENSE.md).
See also [pygame's pinned license description](https://github.com/pygame/pygame/blob/2.6.1/README.rst#license),
[Mozilla's compatibility guidance](https://www.mozilla.org/en-US/MPL/2.0/FAQ/),
and the [current inventory](dependency-licenses.md).

Exact wheel URLs, sizes and hashes are in the
[source-wheel inventory](../scripts/sherpa-source-wheels.json). The lockfile and
[generated dependency inventory](dependency-licenses.md) identify the selected
artifacts. Package metadata alone does not describe every native component.

## Redistribution requirements

The source uses official sherpa/core 1.13.8 and upstream Kokoro with English
eSpeak language data. Complete older caches remain supported. Model weights,
compiled language data, fonts and voice assets keep their separate terms;
source licensing alone does not establish their redistribution rights.

Preserve exact model/conversion/data notices and provide matching compiled
language-data source/build materials. Supertonic's OpenRAIL-M restrictions apply
only to that model. Resolve the exact Qwen intermediary export grant and the
full Kokoro artifact scope before redistributing those files. See
[asset and model guidance](open-source-assets.md) and
[upstream Kokoro notices](../vendor/kokoro/UPSTREAM-NOTICE.txt).

Conveyed native binaries need complete notices and corresponding source/build
materials, together with applicable LGPL relinking/recombination and MPL
preferred-source access. Official upstream wheels do not themselves establish
compliant downstream packaging.
