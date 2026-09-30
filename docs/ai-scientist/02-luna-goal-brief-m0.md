# GPT Luna 6 Goal Brief — SWAPP AI Scientist · M0

**Kapsam:** Çekirdek. Harness, Referee, sandbox, ledger, lokal LLM (S1/S2) ve kamu verisiyle gözetimsiz araştırma döngüsü.
**Spec:** `01-ai-scientist-spec.md` (bu brief onun §7.M0 dilimidir).
**Kullanım:** Spec'i yeni reponun `docs/ai-scientist/` dizinine koyun. Aşağıdaki görev metnini GPT Luna 6'ya goal olarak verin. Her kilometre taşı ayrı bir goal'dür; bu brief yalnız M0'ı kapsar.

## Görev metni (goal olarak yapıştırın)

````text
SWAPP AI Scientist, karpathy/autoresearch desenini santral sensör verisinde anomali tespiti ve
çalışma modu kümelemesine uyarlayan, tamamen lokal dil modeliyle çalışan bir araştırma servisidir.
Bu goal yalnız M0'ı kapsar: kamu veri setleriyle uçtan uca, gözetimsiz bir araştırma döngüsü.

Spesifikasyon: docs/ai-scientist/01-ai-scientist-spec.md. TAMAMINI oku. §2, §3, §7.M0, Ek A ve
Ek C bağlayıcıdır.

"⚠ VARSAYIM" etiketli maddeler doğrulanmamıştır:
- Etkileşimli moddaysan sor.
- Goal (otonom) moddaysan en muhafazakâr seçeneği uygula, gerekçesiyle
  docs/ai-scientist/m0-plan.md'nin "Varsayımlar" bölümüne yaz ve devam et.
- Spec ile çelişen bir repo veya ortam gerçeği bulursan DUR ve raporla.

Kapsam kararları (verildi, tartışmaya açık değil):
- Yeni repo: swapp-ai-scientist. Python 3.12, uv, FastAPI, Postgres 16, SQLAlchemy 2 + Alembic.
  Bu goal'de SWAPP repolarına dokunulmaz.
- Dil modeli LOKAL: Qwen/Qwen3.5-9B, vLLM, tek RTX 4070 Ti Super 16 GB. Bulut LLM çağrısı YASAK.
  S1 = düşünme kapalı, S2 = düşünme açık (spec §3.10). M0'da adaptör yoktur; taban model kullanılır.
- Ajan çatısı: kendi minimal araç döngün (spec §4.2 araç yüzeyi). LangGraph, Codex/Claude Code
  headless veya başka bir ajan çerçevesi YOK.
- Keep/discard kararını Referee verir (Ek C decide()). LLM karar vermez.
- Veri: yalnız kamu setleri ve harness'in sentetik smoke görevi. PI/ONEPACT M1'dedir.
  - SKAB: EVT.
  - TSB-AD-M'nin endüstriyel 4 seti: EVT.
  - CARE to Compare: anomali setleri PDM, normal setler NRM; farm A dev, farm B holdout,
    farm C sealed.
- Metrikler TSB-AD 1.5 evaluation paketinden VENDOR edilir; paket bağımlılık olarak eklenmez.
- Sandbox: Docker. GPU yok, ağ yok, veri dosyası yok (spec §3.2.7 iki fazlı stdin protokolü).

Yapılacaklar:

1. KEŞİF (kod yazmadan):
   - Ortamı ölç: nvidia-smi, sürücü/CUDA, RAM, çekirdek sayısı, OS/WSL2, Docker ve NVIDIA
     Container Toolkit.
   - vLLM'in Qwen3.5-9B'yi sm_89'da sunduğunu dene: FP8, prefix caching, qwen3 reasoning parser,
     qwen3_coder tool parser. S1 ve S2 için token/s ölç; 32k bağlamda 2 eşzamanlı dizinin sığıp
     sığmadığını ölç.
   - Unsloth ile 9B QLoRA'nın 24k dizide birkaç adımlık tepe VRAM'ini ölç.
   - Kamu veri setlerini indir; lisanslarını ve görev eşlemelerini doğrula. TSB-AD-M'den endüstriyel
     4 seti seç.

2. PLAN: docs/ai-scientist/m0-plan.md yaz. İçerik: bulgular, ölçümler, dosya listesi, gerekçeli
   spec sapmaları, varsayımlar. Etkileşimli modda onay bekle; goal modunda yaz ve devam et.

3. UYGULAMA (her adım ayrı commit, bu sırayla):
   a. Repo iskeleti: pyproject.toml, uv.lock, scripts/quality_gate.py.
   b. harness/contracts.py (Ek C ile birebir), CONTRACT.md, VERSION, vendored
      metrics/tsb_ad_eval ve golden fixture'lar (7.M0.1).
   c. Kamu yükleyicileri, snapshot manifesti (§2.1), split ve embargo (§3.2.3), enjeksiyon
      (§3.2.4), süit kurucu (suite_weights, aile tavanı).
   d. Guard'lar (§3.2.6): harness_hash, interface, degenerate, causality, determinism,
      position_bias, hardcoding, timeout/oom, forbidden_access.
   e. Scorer (ayrı süreç), Referee (decide, iki aşamalı teyit, noise floor, KEEP_SIMPLER sınırı)
      ve replay.
   f. LocalDocker runner ve Dockerfile.sandbox: uid ayrımı, stdin Arrow IPC, P=4, bütçeler.
   g. Postgres ledger, Alembic, experiment.v1 / trajectory.v1 yazımı, sha256'lı yerel blob deposu.
   h. lab/llm: OpenAI uyumlu backend, ops/ altında vLLM servis tanımı, S1/S2 router
      (route_system), egress kilidi, gpu_lease (SERVE ↔ TRAIN noop).
   i. lab/agent: bağlam kurucu (§3.3.3 sırası, inputs_hash), araçlar (§4.2), bölüm döngüsü,
      araç çağrısı doğrulama + tek onarım turu, hata redaksiyonu.
   j. Director: durum makinesi, bütçeler, plato/EXPLORE, devre kesici, holdout kontrolü (yalnız
      bit), Thompson bandit.
   k. CLI: lab doctor | baseline | run | report | replay | gpu.
   l. Baseline'lar (robust_z, iforest, ecod) ve program/program_ad.md (Ek A ile birebir).
   m. Uçtan uca testler: sahte LLM ile 20 deneylik gözetimsiz koşu (7.M0.9) ve gerçek lokal
      LLM ile duman koşusu (7.M0.13).

4. KALİTE KAPISI (her commit öncesi): python scripts/quality_gate.py
   - İçerik: ruff, pylint, bandit, pytest, mypy --strict (harness/, lab/referee/,
     lab/llm/router.py).
   - Exit kodu 0 değilse kapı yeşil değildir.
   - Çıktıyı pipe'lama; pipe exit kodunu maskeler.
   - GPU testleri @pytest.mark.gpu ile işaretlenir ve kapının dışında, lab doctor ile koşulur.

5. CHANGELOG.md: bugünün tarihi altına tek bir madde ekle.

6. GIT: main'den bir feature branch aç. PR'ı --base main ile aç. MERGE ETME.

Teknik kurallar:
- Etiketler sandbox'a ASLA girmez.
- Veri sandbox'a dosya olarak mount edilmez; fit ve score ayrı süreçlerde koşar.
- TSB-AD'nin eşiğe bağlı metriklerini ASLA pred=None ile çağırma; kütüphane oracle eşik seçer.
- Zaman karşılaştırmaları parse edilmiş anlık üzerinden, tz-aware UTC ile yapılır.
- Meta-feature'lar yalnız train girdisinden hesaplanır; etiketten türeyen özellik yoktur.
- Ajana holdout sayısı gitmez; yalnız "geçti" / "geri alındı" biti gider.
- Bağlam önek-kararlı sırada kurulur. Bağlam ≤ 16k token; çıktı S1 ≤ 2k, S2 ≤ 8k.
- llm.base_url loopback ya da iç ağ değilse lab doctor başarısız olur.
- Sandbox konteynerine --gpus verilmez.
- Sabit tohum random_state=0; thread sayıları 2'ye sabitlenir.
- Harness'i değiştiren her commit harness VERSION'ını artırır. Golden fixture'lar doğrulanır,
  yeniden üretilmez.

Bitti tanımı: spec §7.M0.1–7.M0.15'in her biri için docs/ai-scientist/m0-acceptance.md'de kanıt
bulunur: komut, çıktı özeti, dosya yolu. Kanıtı olmayan madde bitmiş sayılmaz.
````

## Hazırlık kontrol listesi

- [ ] `swapp-ai-scientist` reposu kurumsal GitHub'da açıldı; spec `docs/ai-scientist/` altında.
- [ ] GPU host: RTX 4070 Ti Super; güncel NVIDIA sürücüsü ve CUDA ⚠ sürüm; Ubuntu 24.04 ya da WSL2.
- [ ] Docker + NVIDIA Container Toolkit kurulu. Yalnız vLLM konteyneri GPU alır; sandbox GPU'suzdur.
- [ ] Postgres 16 yerelde çalışıyor.
- [ ] `Qwen/Qwen3.5-9B` ağırlıkları indirildi; ≥ 60 GB boş disk var.
- [ ] vLLM sürümü: Qwen3.5 destekli son kararlı sürüm ⚠. Keşifte pinlenir.
- [ ] Kamu veri setlerine erişim ve lisans kontrolü tamam (SKAB, TSB-AD, CARE).
- [ ] Host ≥ 64 GB RAM ve ≥ 12 çekirdek ⚠.
- [ ] Gece koşuları için güç ve ısı onayı alındı.

## Beklenen çıktılar

- PR: M0 kodu, `m0-plan.md`, `m0-acceptance.md`, CHANGELOG girdisi.
- Bir gece koşusunun `lab report` çıktısı: merdiven grafiği, deney sayısı, KEEP/DISCARD/REJECT dağılımı.
- `lab doctor llm` ve `lab doctor train --dry-run` ölçümleri. Bunlar §3.10.6 VRAM tablosunu gerçek değerlerle günceller; sapmalar ADR olur.

## Sonraki goal'ler

- M1 brief'i, M0 PR'ı merge edildikten sonra yazılır.
- M0'daki `m0-plan.md` sapmaları ve VRAM ölçümleri M1 spec güncellemesine girer.
- Spec'in GPT Astra 6 review bulguları M1'den önce işlenir.
