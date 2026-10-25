# Whisperstream Open-Source License and Processing Notices

**Version 2.1**

**Effective 2026-10-22**

This notice explains the open-source edition's software rights, processing
acknowledgment, and separately licensed models and services.

## 1. Software license and attribution

Lanreal Technologies Inc. licenses its original contributions in this edition
under the GNU General Public License, version 3 only (GPL-3.0-only). The complete
license accompanies the source and distribution in LICENSE. You may use, study,
modify, copy and redistribute the covered software, including commercially,
under that license. Distribution of covered binaries requires compliance with
GPLv3, including its corresponding-source requirements. The license is also
available at https://www.gnu.org/licenses/gpl-3.0.html.

Corresponding Source for each official installer, including the scripts used to
build it, is available without charge at
https://github.com/lanreal-io/whisperstream at the release tag matching the
installed version. You do not need to submit
the installer request form to obtain it.

Whisperstream was developed by Lanreal Technologies Inc., building on Will
Hampson's Chirp project. The original Copyright (c) 2025 whamp MIT notice remains in
LICENSES/Chirp-MIT.txt. That upstream MIT grant remains available for the upstream
material. NOTICE identifies the source grant and attribution. Third-party
components keep their own licenses, reproduced in THIRD_PARTY_LICENSES.txt and
component-specific directories. The app's source grant does not relicense model
weights, fonts, recordings or other separately identified assets.

You are not required to accept the GPL merely to receive or run a copy. This
notice adds no restriction to the rights granted by the GPL or other component
licenses. In particular, it does not prohibit modification, reverse engineering,
repackaging, redistribution, commercial use, competitive development or private
use. GPL termination and reinstatement are governed by the GPL itself, not by a
separate proprietary license condition. No trademark license or permission to
imply official endorsement or sponsorship is granted (GPLv3 section 7(e)).

## 2. Processing acknowledgment and local controls

The provided app asks you to review this notice before it records, transcribes,
cleans up text or speaks content. Clicking the acknowledgment records the exact
notice version and hash, event type, app version and time in the local acceptance
ledger under the configured app data directory. Earlier evidence is preserved.
This is a control over processing in the provided app, not an additional
condition on the software license or a restriction on modifying the app. The
official Windows installer displays the GPL (LICENSE) as its license page; this
notice is acknowledged in the app on first run and when it changes, not during
installation.

Cloud-provider consent, optional model terms and Lockdown controls are separate
from the GPL grant. Accepting this notice alone does not enable optional cloud
cleanup or optional speech, and does not consent to unrelated future processing.
The functionality included in this edition is free of charge and requires no
activation, license key, trial or device-count allowance.

## 3. Privacy and network behavior

The app processes speech recognition locally. Imported audio is decoded locally.
The bundled Privacy Policy (PRIVACY.txt) describes data handling. Model
downloads use pinned public Hugging Face sources, with verified upstream GitHub
backups for supported speech models. Kokoro downloads use Hugging Face only.
Compatible cached models remain usable. Downloads send connection metadata,
including the IP address, ordinary HTTP headers and requested file URL, to the
receiving host. They do not send audio or transcripts. File and archive
integrity checks remain in place.

This notice and the Privacy Policy apply however you get the app. Built from
source, the app makes no application-update requests; update it by
rebuilding from newer source. Installed from the official installer on the
Whisperstream website, the app makes one more request. It checks for signed
updates at most about once a day, or when you check from About, and installs
an update only after you accept it. Update requests go to Lanreal storage
hosted on Cloudflare R2. They send connection metadata, including the IP
address and ordinary HTTP headers, but no audio, transcripts, API keys or
account or device identifiers. Lockdown stops them.

Opening an external link is a separate action. Bug reports and feature requests
open GitHub Issues in your browser. Issues and attachments are public and
governed by GitHub's terms. The app does not upload your content automatically.
The app does not activate or validate licenses, or submit in-app ratings,
reviews or feedback.

Optional cloud cleanup sends the selected text and configured instructions to
the provider you choose only after the required consent. Your provider's terms,
privacy practices and charges apply to that service. Local cleanup stays on your
device. Lockdown blocks the app's cloud-cleanup path. Agent Speak communicates
locally with connected coding assistants; their own network activity is governed
by their separate provider settings and terms. Text insertion into another app
is user-directed delivery to that destination.

## 4. Models and other separately licensed assets

Models are downloaded separately and retain their own governing licenses and
notices. The GPL source grant does not override those terms or grant rights to
redistribute an artifact whose license has not been established. Preserve model
names and attribution. Consult THIRD_PARTY_LICENSES.txt and the model's delivered
license before redistributing model or language-data files.

Stream-1 Mini by Whisperstream, the default local cleanup model, is Lanreal's
fine-tune of Qwen/Qwen3-0.6B-Base, Copyright 2024 Alibaba Cloud, licensed under
Apache-2.0. Stream-1 Mini is offered under the Stream-1 Mini License, which
combines Apache-2.0 terms with one additional condition: when you use, distribute
or integrate Stream-1 Mini, or a model derived from it, you must keep a reasonably
accessible identification of it as "Stream-1 Mini" by "Whisperstream" and
identify modified versions as modified. That license, the Apache-2.0 text and
the model NOTICE are published with the model at
https://huggingface.co/whisperstream/stream-1-mini. The condition does not
restrict rights in the original Qwen work or the GPL-covered application source.

The upstream Kokoro model and lexicon retain their Apache-2.0 terms. Its
supporting eSpeak language data is GPL-3.0-or-later, and the official native
runtime includes separately identified eSpeak/Piper components. Other supporting
assets retain their recorded notices. These component licenses and notices must
be preserved when distributing the respective files. The Supertonic restrictions
below do not apply to Kokoro or to the GPL-covered application source.

## 5. Optional Supertonic model terms

Supertonic 3 is Copyright (c) 2026 Supertone Inc. and is offered under the
BigScience Open RAIL-M License (OpenRAIL-M). It is an optional, separately
downloaded model, called the Restricted Voice Model below. Its full license,
including Attachment A, is reproduced in THIRD_PARTY_LICENSES.txt. Its upstream
model card is https://huggingface.co/Supertone/supertonic-3.

These terms apply only if you choose to download, enable or use the Restricted
Voice Model. By doing so, you agree to its OpenRAIL-M terms, including the use
restrictions below. These are model conditions, not additional restrictions on
the GPL-covered software. Declining or losing permission to use this model does
not terminate your GPL rights in Whisperstream.

**Your Supertonic use restrictions.** If you download, enable, or use the
Restricted Voice Model, you agree that you will not use the Restricted
Voice Model, and will not permit any third party to use the Restricted
Voice Model:

(a) in any way that violates any applicable national, federal, state,
local, or international law or regulation;

(b) for the purpose of exploiting, harming, or attempting to exploit or
harm minors in any way;

(c) to generate or disseminate verifiably false information or content
with the purpose of harming others;

(d) to generate or disseminate personally identifiable information that
can be used to harm an individual;

(e) to generate or disseminate information or content (for example,
images, code, posts, or articles), or to place that information or
content in any context (for example, a bot generating social media
posts), without expressly and intelligibly disclaiming that the
information or content is machine generated;

(f) to defame, disparage, or otherwise harass others;

(g) to impersonate or attempt to impersonate any person or entity,
including by means of synthetic media commonly described as deepfakes;

(h) for fully automated decision-making that adversely impacts an
individual's legal rights or otherwise creates or modifies a binding,
enforceable obligation;

(i) for any use intended to, or having the effect of, discriminating
against or harming individuals or groups based on online or offline
social behaviour or known or predicted personal or personality
characteristics;

(j) to exploit any of the vulnerabilities of a specific group of persons
based on their age or their social, physical, or mental
characteristics, in order to materially distort the behaviour of a
person belonging to that group in a manner that causes or is likely to
cause that person or another person physical or psychological harm;

(k) for any use intended to, or having the effect of, discriminating
against individuals or groups based on legally protected
characteristics or categories;

(l) to provide medical advice or to interpret medical results; or

(m) to generate or disseminate information for use in the
administration of justice, law enforcement, immigration, or asylum
processes, including predicting that an individual will commit fraud or
a crime, whether by text profiling, by drawing causal relationships
between assertions made in documents, or by indiscriminate or
arbitrarily targeted use.

The full Attachment A delivered in THIRD_PARTY_LICENSES.txt is incorporated
into these model terms. Where its restrictions are broader than this rendering,
Attachment A governs use of the Restricted Voice Model. Nothing here narrows
that model license. Model-license termination and other rights are governed by
the model's license. Supertone and other third-party licensors do not become
parties to an additional support or warranty commitment by being identified here.

## 6. Warranty, support and preserved rights

To the extent permitted by applicable law, the GPL-covered software, the
official installer and Lanreal's download services are provided
"AS IS", WITHOUT WARRANTY OF ANY KIND, INCLUDING MERCHANTABILITY OR FITNESS FOR
A PARTICULAR PURPOSE, and Lanreal is not liable for damages arising from their
use, as further set out in GPLv3 sections 15-17. Mandatory consumer rights that
cannot be excluded are unaffected. Third-party components have their own
warranty and liability terms. Lanreal does not promise ongoing support,
development or a release schedule. Review generated text and audio before
relying on them; the app is not a certification of compliance for any regulated
workflow.

This notice does not modify any separate agreement you may have with Lanreal,
and does not waive, shorten or replace any source-code, relinking or other
obligation that applies to software you received. Permissions you already hold
under the GPL, MIT or other component licenses and mandatory legal rights remain
unaffected. No release of claims is imposed here.

## 7. Contact and future versions

Questions may be directed to
support@whisperstream.io. Contributions and source issues follow the repository's
CONTRIBUTING.md and SECURITY.md. A future notice cannot withdraw rights already
granted under the GPL. Lanreal's future notices for the official app will keep
the versioned acknowledgment and consent mechanisms. This is a commitment by
Lanreal, not a requirement on modified versions.
