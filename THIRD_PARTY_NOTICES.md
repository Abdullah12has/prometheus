# Third-party components

Permetheus uses these separately licensed components. Model weights and runtime caches are not included in this repository.

- Pocket TTS library: MIT, https://github.com/kyutai-labs/pocket-tts/blob/main/LICENSE . English model revision `39592ff23c9ef80098bb74895d104c26275fe2c9`; model access and license terms apply separately: https://huggingface.co/kyutai/pocket-tts . Bundled voice profiles retain their upstream terms.
- transcribe-cpp Rust binding and native engine: see locked crate license and https://github.com/handy-computer/transcribe.cpp . The native bridge uses the public API; it does not incorporate another application's UI or data.
- NVIDIA Nemotron 3.5 ASR: OpenMDW-1.1, https://openmdw.ai/license/1-1/ . Local GGUF conversion revision `6d44e540bc31b0de1dbe174a3cea87f53a7f22fb` declares the same license and pins upstream commit `24b151a`: https://huggingface.co/handy-computer/nemotron-3.5-asr-streaming-0.6b-gguf . Preserve applicable license, attribution and notices when distributing weights.
- SearXNG: AGPL-3.0-or-later; runs as an unmodified separate container. Source: https://github.com/searxng/searxng .
- PostgreSQL: PostgreSQL License, https://www.postgresql.org/about/licence/ .
- PRH / Finnish Business Information System: public company data is attributed to its source. Permetheus is not affiliated with PRH. https://avoindata.prh.fi/en .

Voice cloning requires permission from the voice owner. A saved voice profile records that authorization; the operator supplies the sample.
