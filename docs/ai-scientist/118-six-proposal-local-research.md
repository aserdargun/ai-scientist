# Altı önerili gerçek yerel araştırma

2026-10-02. Koşu `e5ec820a-8abb-4100-ba89-3f210209c6ed` **completed**.
Admission → terminal süre **934,062 saniye** (15 dakika 34 saniye).
Gerçek yerel Qwen, altı öneri üretti; Docker deneyleri ve bağımsız Scorer
15 ölçümü tamamladı. Bu, tek sentetik snapshot üzerinde yerel model
araştırmasıdır. Endüstriyel doğrulama veya AOS ortak kabulü değildir.

## Raporu açma

Açık konsolda **Kabul durumu → M0.13 → mode-six-research-20261002.html**.
Bu kayıt, deney API bağlantısı olmadan mevcut kanıt görüntüleyicisinde açılır.
Ana konsol bu gözlemde yalnız durum/kanıt gösterir; deney API'si bağlı değildir.

- [Bağımsız üretilmiş HTML raporu](review-evidence/mode-six-research-20261002.html)
- [Yapılandırmalar, ölçümler ve kapanış kanıtı](review-evidence/mode-six-research-20261002.json)

## Geçen maddeler

- **S1×2 + S2×4**, altı kayıtlı öneri ve altı bağımsız aday puanı.
  LSH×3, SOM×2, OPTICS×1; altı normalize yapılandırma birbirinden farklı.
- Üç baseline × üç seed = **9 başlangıç ölçümü**, ardından **6 aday ölçümü**.
  Etiketler model bağlamına verilmedi. Sonraki öneriler önceki ölçülmüş
  geri bildirimi kullandı; bağlamdaki geri bildirim sayıları 0→5 ve hash'leri doğrulandı.
- Altı yanıt strict şemadan ve aynı host compiler'dan geçti. **0 ayrıştırma
  hatası, 0 onarım, 0 fallback**; altı started/completed provider çifti eşleşti.
- **109 checkpoint ve 15 Scorer artefaktı** doğrulandı. Altı Referee kararı,
  düzeltilmiş salt okunur replay okuyucusuyla birebir yeniden hesaplandı.
- Özgün bütçe **6 öneri / 3600 s / 180000 token**. Gerçek kullanım
  **5478 giriş + 2800 çıkış = 8278 token**. Son bütçe checkpoint'i
  930,872 s; açık rezervasyon yok. Süre veya yetki uzatılmadı.
- Canonical scheduler fencing tokenları **26–31**, altı exact request `done`.
  Kayıtlı model PID/GPU çocukları, cgroup ve UDS yolları yok; Director,
  15 Scorer unit'i ve geçici API kapalı. Özel DB dump/TOC doğrulandı ve
  yalnız bu koşunun PostgreSQL konteyneri kaldırıldı. Eski koşulara dokunulmadı.

| Öneri | Sistem | Yöntem | Hareket | VUS-PR | Karar |
|---|---|---|---|---:|---|
| 1 | S1 | LSH | hparam | 1,0 | DISCARD |
| 2 | S1 | LSH | preprocess | 1,0 | DISCARD |
| 3 | S2 | SOM | features | 1,0 | DISCARD |
| 4 | S2 | SOM | regime | 1,0 | DISCARD |
| 5 | S2 | OPTICS | detector | 1,0 | DISCARD |
| 6 | S2 | LSH | fusion | 1,0 | DISCARD |

Baseline champion bu basit örnekte zaten 1,0 puandaydı. Altı adayın delta
ve CI değeri 0; champion değişmedi. Koşu iyileşme veya öğrenilmiş adapter
kanıtı değildir. Snapshot 192 eğitim, embargo sonrası 86 değerlendirme
satırı ve dört sensör içerir; tek görevden genelleme sonucu çıkarılmaz.

## Kaynak, model ve ölçülen maliyet

Yürütme Scientist `55c5300600112ab823f76ec434029d6dc23e513c`, harness
`45acba391c2c32184339ae564d7bb7e0ef4e5fda247beb66bfe3c431e9cb69b9`, sandbox
`sha256:e12dd9e6c660a95bf37d5db8dc9b0156b483a847a66cd712b5daf8aa0850ea85`.
AOS salt okunur referansı `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`;
AOS servis/model çağrısı yapılmadı. Son yerel diff hash'leri JSON kanıtında tutulur.

Sözleşmeler `operating-mode-config.v1`, `local-qwen-provider-config.v2`;
config SHA `2303d46a7dcd22e942eacabc19e20478ecbfa7c0c1c856b6c5cd83be1f0cd210`.
Qwen/Qwen3.5-9B revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`,
**FP8 per_tensor**; vLLM 0.30.0 / torch 2.13.0+cu132 / transformers 5.17.0.

| Profil | Bağlam üst sınırı | Çıktı üst sınırı | Model max | Thinking |
|---|---:|---:|---:|---|
| S1 | 8192 | 2048 | 10240 | Kapalı |
| S2 | 8192 | 8192 | 16384 | 512 token |

Gözlenen tepe VRAM **12890 MiB**. Altı çağrının toplam startup süresi
461,717 s, inference 85,158 s, drain 4,588 s. Model cgroup bellek tepesi
10 GiB; bu tüm host'un veya birlikte çalışan iki projenin RAM tepesi değildir.
GPU her çağrı sonunda bırakıldı. Kuyruk bekleme ve AOS devir gecikmesi ayrı
ölçülmedi; JSON'daki submit→runtime-binding aralığı kuyruk beklemesi diye sunulmaz.

SQL rapor SHA `20181fc5bfa6d1c3e5e9b2f34799da834d3eaa772d122be361073e4afca37a9c`.
HTML SHA `3609072fc5628b0d754a90a3ae7d070396ce0ce6b87c451c5c09b6e51e3a85a1`.

## Gerçek hata ve dar düzeltme

Araştırma dispatch'i exit0 ile tamamlandı. Ardından özel gözlem betiği,
Scorer artefaktını varsayılan klasörde aradığı için exit1 verdi; DB korundu.
Ayrı readback, ürünün `lab/replay.py` dosyasında da aynı eksikliği yakaladı:
hem Scorer çıktısı hem NRM guard okuması mevcut `artifact_root` parametresini
iletmiyordu. Bu iki okuma `feat/omr-stream-v1` dalında düzeltildi.

Gerçek dosyalı EVT/NRM regresyonu önce başarısız oldu; düzeltmeyle sekiz
odaklı test geçti. Özel kökte olmayan artefaktın varsayılan kökten
tamamlanamayacağı da doğrulandı. Kontroller gevşetilmedi.
Yeni replay kaynak SHA
`18f754e3ec29c9e75b9631d72189c4c49ce40b3f53c00d35efc9ba48f3e4fdae`.
Bu okuyucu yalnız bitmiş koşunun kayıtlarını doğrulamak için kullanıldı;
deney/model tekrar çalıştırılmadı, native ana kaynak pinleri değiştirilmedi.
Gözlemci hatası ve eski replay başarısızlığı tarihsel kanıtta korunur.

## Kalan ve çalıştırılmayan

- **Kalan:** gerçek AOS görev/devir/araştırma/rapor doğrulama ve adil
  birlikte ilerleme; kaynak/config inceleme yanıtı bekleniyor.
- **Kalan:** seçilmiş public kaynaklarda araştırma ve bağımsız holdout.
  Bu koşunun holdout'u `not_run`, manual review gerekli.
- **Çalıştırılmadı:** 32k×2 kapasite, tam host egress denetimi,
  eğitim/LoRA/QLoRA ve bu koşuda ayrı inflight iptal senaryosu.
  Önceki iptal kanıtı `106-mode-agent-and-inflight-stop.md` içindedir.
- **Ayrı işler:** proje lisansı, genel CI, taşınabilir kurulum ve açık
  çekirdek teslimi. Merge/push/deploy yapılmadı. M0.13 ve bütün goal kısmi kalır.

## Teslimat kalite kapısı

Ayrı ürün dalının harness sürümü **0.43.1**, hash'i
`a9b0b56e718752332eb05a816ca767b79618dfb40f77fb19cc0e4e469c555598`; paket sandbox image'i
`sha256:f6658fa5727f132fc457b779c1e716b16641731ecf795a59efe88ab135d91065`. Bu paket, yukarıdaki bitmiş
araştırmanın kaynak/image pinlerinden ayrıdır; ana native kaynak değiştirilmedi.

2026-10-02 13:58 UTC kapısı: **3200 passed / 7 skipped / 177 deselected**,
222,52 s. Ruff, Pylint, Bandit, CPU pytest, strict mypy, wheel build ve
wheel import kontrollerinin **yedisinin exit code'u 0**. Atlanan yedi test
ayrı opt-in PostgreSQL recovery ortamını gerektirir; GPU/live testleri bu
paket kapısının kapsamı dışındadır. Önceki ilk deneme `/tmp` disk rezervi
ve eksik `uv` PATH'i; ikinci deneme fazla uzun Unix socket yolu nedeniyle
başarısızdı. Kısa, ayrı disk dizini ve doğru PATH ile aynı kapı başarıyla
tekrar çalıştı. Test veya kaynak güvenliği sınırları gevşetilmedi.

Kaynak/diff hash'leri JSON kanıtında tutulur. Düzeltme yerel ürün dalında
teslim edilir; ana native checkout ve AOS kaynakları değiştirilmez.
