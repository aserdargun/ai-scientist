# Director referansları ve ECOD nedensellik kararı

Tarih: 2026-09-24. Başlangıç commit'i `2c76c37`, harness `0.8.0`.

## ECOD kaynak incelemesi

Spec §3.2.2, robust-z / Isolation Forest / ECOD sonuçlarıyla görev bazında dondurulmuş normalizasyon ister. Aynı spec ve review eki, fit bilgisinin değerlendirme sırasında değişmemesini ve geleceğin önceki skorları etkilememesini zorunlu kılar.

İncelenen [resmî PyOD ECOD kaynağı](https://github.com/yzhao062/pyod/blob/109bdfd14d337875c707ce33c50d168dc84d756c/pyod/models/ecod.py#L138), `decision_function` çağrısında eğitim ve değerlendirme dizilerini birleştirip ECDF ile çarpıklığı yeniden hesaplar. Bu, mevcut Lab nedensellik sözleşmesiyle uyuşmaz. Kaynak commit'i `109bdfd14d337875c707ce33c50d168dc84d756c`; dosya SHA-256 değerleri [ölçüm kaydında](review-evidence/ecod-causality-review.json) bulunur. Kaynağın [BSD-2-Clause lisansı](https://github.com/yzhao062/pyod/blob/109bdfd14d337875c707ce33c50d168dc84d756c/LICENSE) ayrıca hash ile kaydedilmiştir.

## Ölçülen karşı örnek

[`review_ecod_causality.py`](review-evidence/review_ecod_causality.py), kaynağı hash ile doğrulayıp yalnız incelenmiş `decision_function`, `column_ecdf` ve eşit değer yardımcısını çalıştırdı. NumPy/Scipy dışında PyOD paketi veya diğer upstream kod çalıştırılmadı; `n_jobs=1` kullanıldı. Bu, bütün PyOD paketinin runtime kabulü değildir.

Eğitim dizisi ve değerlendirme dizisinin ilk üç satırı sabit tutuldu. Yalnız sonraki dört değerlendirme satırı değiştirildi:

| İlk üç skor | Değerler |
|---|---|
| İlk değerlendirme | 1.0986122886681096, 0.8109302162163288, 0.6931471805599453 |
| Gelecek satırları değiştirilince | 0.6931471805599453, 0.9444616088408515, 1.2809338454620642 |

Maksimum bağıl prefix farkı **0.5350264792820728** oldu; Lab guard sınırı `1e-7`. Komut: `OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_ecod_causality.py`; exit 0, karşı örnek doğrulandı. Girdiler ve kaynak URL/hash'leri JSON'da tam olarak bulunur. Yeniden çalıştırmak için bu kaynak dosyaları `data/runtime/ecod-reference-review` altında bulunmalıdır; bu dizin git dışındadır.

## Uygulama kararı ve açık doğrulama

Baseline'ın ECOD kolu eğitimde sabitlenen ECDF sıraları ve çarpıklık yönünü kullanan **açıkça adlandırılmış/sürümlenmiş bir ECOD uyarlaması** olmalıdır. Kaynak sürümüne eşdeğer sonuç verdiği iddia edilmez. Uyarlama:

- Eğitim istatistiklerini yalnız `fit(train)` sırasında hesaplar.
- Değerlendirme satırını kendi değerleri ve değişmez eğitim istatistikleriyle skorlar; eval batch boyutundan veya sonraki satırlardan öğrenmez.
- Eşit değerleri, eğitim dışı kuyruk değerlerini ve sıfır olasılık için sonlu alt sınırı açık bir sözleşmeye bağlar.
- Alarm eşiğini yalnız eğitim skorlarından üretir.
- Algoritma adı/sürümü, parametreleri, kaynak atfı ve harness hash'i baseline ve replay kayıtlarında görünür.

Bu karar üretim uyarlamasının tamamlandığı anlamına gelmez. Luna uygulayacak; bağımsız train/eval sınırı, kuyruk/eşitlik, nedensellik ve determinism kontrolleri yapılacak. Aynı gerçek Docker/Scorer yolu robust-z ve Isolation Forest için de çalıştırılmadan `base/ref` dondurulmaz.

Director değerlendirmesinde `base=robust-z`, `ref=max(robust-z, IForest, sürümlü ECOD uyarlaması)` olacak; tipin payda tabanı ve `[-1,3]` kırpma korunacak. Baseline 0–2, aday ilk değerlendirme 0, KEEP teyidi 1 ve bootstrap 0 ayrı kimliklerle kaydedilecek. Teyit bütçesi yetmiyorsa KEEP kesinleşmeyecek.

## Director ledger taslağı incelemesi

`0011` öncesi taslak incelemesi; aşağıdakiler henüz uygulanmış veya gerçek PostgreSQL üzerinde doğrulanmış kabul edilmez:

- Hipotez, `predicted_delta`, kaynak/blob/input hash'leri ve parent kimliği ilk guard/fit/score işleminden **önce** değişmez olarak kaydedilmeli. Baseline çalışmaları da ölçümden önce kimlik kazanmalı. Güncelleme ve yeniden başlatma yolları sonuçtan sonra hipotez değiştirememeli.
- Yeni mutasyonlar mevcut görev planlama sırasını korumalı: advisory lock → run row → job/experiment row. Aynı satırlara ters sırayla kilit alan servisler eklenmemeli.
- Director'a yalnız açıkça `dev` olarak sınıflandırılmış, tam görev/profil/aday kimliğiyle eşlenmiş sonuçlar görünmeli. Visibility belirtilmemiş/eski profil varsayılan olarak gizli kalmalı; sonradan sınıf değiştirme veya profil değiştirme geçmiş skoru yeniden sınıflandıramamalı.
- Taslakta görülen `dataset_profiles.visibility` varsayılanı `dev` bu gerekliliğe aykırıydı; düzeltme Luna'ya iletildi. Director özel profil/etiket erişimi kazanmaz. Planner görev planlamak için profil kimliklerini kullanır; etiket veya ham seri okuyamaz.
- Yalnız dev sonuç view'ını filtrelemek yeterli değil: `2c76c37` sürümünde `lab/scorer/service.py` finalizer'ı tüm `task_scores` ve terminal görev kimliklerini `reports.report_json` içine koyuyor. `lab/api/app.py` doğrulanmış rapor uç noktası aynı JSON'u döndürüyor. Bu yol, holdout görevleri eklendiğinde skor/kimlik sızdırır. Rapor ve hata/terminal çıktı yolları da görünürlük sınırını korumalı; holdout için daha sonra yalnız izin verilen karar biti açılmalı. Bu bulgu kaynak incelemesidir; mevcut gerçek holdout çalışması veya sızıntı deneyi değildir.
- Ölçülmeyen FA/day ve event-F1 alanları null kalmalı; `simpler` kararı LLM beyanından alınmamalı, güvenilir kod/süre ölçümlerine dayanmalı. Trajectory ve eğitim dışa aktarımı `noncommercial_research` tercihini ve kaynak lisans etiketlerini korumalı; scrub bayrakları yalnız gerçekten uygulanmış temizleme sonucunu göstermeli.

Bu maddelerin uygulama, rol yetkisi, yeniden başlatma ve rapor sınırı kanıtları açık. M0 kabul tablosunun durumları bu taslak incelemesi nedeniyle kapatılmadı.

### Taslak SQL karşı örnekleri

`0011` uygulanmadan, mevcut `0010` PostgreSQL üzerinde yalnız `SELECT` sorgularıyla iki bulgu yeniden üretildi; hiçbir şema/veri mutasyonu yapılmadı. [SQL ve sonuç kaydı](review-evidence/director-draft-sql-semantics-review.json), incelenen taslağın hash'ini ve tam sorguları içerir:

1. Yeni experiment UUID kimliği ile mevcut `run_tasks.experiment_id` varchar kimliğinin doğrudan eşitlik karşılaştırması `42883` (tanımsız operatör) verdi. Ledger, görev ve belge kimlikleri aynı canonical temsile bağlanmalı.
2. `kind='baseline'`, `experiment_number=NULL`, `baseline_name=NULL` satırı mevcut kimlik CHECK ifadesinden NULL üretti; bu değer PostgreSQL CHECK tarafından kabul edilir. Baseline adı açık `IS NOT NULL` şartıyla zorunlu olmalı.

Ek kaynak incelemesinde terminal record trigger'ının hipotez/input/parent alanlarının tamamını karşılaştırmadığı, trajectory'nin run/outcome/blob eşliğinin eksik olduğu ve stop istenince terminal experiment geçişlerinin reddedildiği görüldü. Düzeltmeler uygulama ajanına iletildi; yeni migration ve servis üzerinde gerçek rol/yaşam döngüsü kontrolleri yapılmadan giderilmiş sayılmaz.

### Sayısal kimlik, kilit anahtarı ve ilk fixture kontrolü

[Float/kilit karşılaştırma kaydı](review-evidence/director-draft-float-and-lock-review.json), mevcut PostgreSQL üzerinde yapılan salt okunur sorguları tutar. `0.0`, `-0.0`, `1.0`, `-1.0` ve `1e-7` değerlerini JSON metni ile PostgreSQL float metni olarak karşılaştırmak geçerli kimlikleri reddetti; sayısal JSONB eşliği altı kontrolün tamamında doğru sonuç verdi. Python görev planının SHA-256 tabanlı kilit anahtarı için SQL karşılığı beş UUID üzerinde aynı 64-bit değeri verdi; negatif işaretli anahtar da kapsandı. Bunlar SQL ifade kontrolleridir; gerçek kilit beklemesi veya migration kabulü değildir.

`OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python -m pytest -q tests/test_director_contracts.py tests/test_api_and_scorer.py` kontrolü **15 passed, exit 0** verdi. [Kaynağa bağlı çıktı](review-evidence/director-contract-report-fixture-review.json), incelenen dosyaların test sırasında değişmediğini doğrular. Kapsam strict kayıt şemaları ve SQLite/in-process API/Scorer rapor filtrelemesidir. Gerçek PostgreSQL rolleri, 20 deneyli Director, blob/replay, LLM veya holdout kabulü yerine geçmez.

Director'ın tam experiment/trajectory tablolarına genel SELECT yetkisi verilmesi gerekmiyor. Dar receipt fonksiyonu yalnız terminal kimlik/digest bilgilerini döndürmeli. Report/replay daha sonra bu digest'lerle gerçek dev belgelerini güvenli blob okumasıyla doğrulamalı; CLI'ya migrator veya Scorer kimliği verilmez. Holdout/sealed metrik ve ham mesajlar dışa aktarılabilir trajectory'ye hiç yazılmamalı; yalnız SQL SELECT'i kapatmak blob dışa aktarımını korumaz.

## Uygulanan 0011: gerçek PostgreSQL kontrolü

`alembic upgrade head`, `0010_terminal_recovery_fence → 0011_experiment_ledger` için exit 0 verdi. Metadata'daki parent FK `ondelete='RESTRICT'` eksikliği düzeltildikten sonra `alembic check` exit 0 oldu. Kontrol öncesinde bu projeye ayrılmış veritabanında runs/task/profile/pending-job sayıları sıfırdı; başka servis veya veritabanı değiştirilmedi.

Bağımsız root kontrolleri:

- `.venv/bin/python docs/ai-scientist/review-evidence/review_experiment_role_surface.py` → **38/38, exit 0**. [Sonuç](review-evidence/experiment-role-surface-review.json). Director doğrudan proposal/durum/sonuç belgesi yazamaz; tam deney/trajectory JSON'u ve özel etiketleri okuyamaz. Yetkili SQL girişlerini yalnız Director çağırabilir; Planner/Scorer bu girişlere sahip değildir. Bunlar ayrı rol kimlik doğrulaması ve grant kontrolleridir.
- `OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_experiment_ledger.py` → **32/32, exit 0**. [Sonuç](review-evidence/experiment-ledger-review.json). Ölçüm öncesi öneri kaydı, aynı önerinin tekrarı, değişen kimlik/hipotezin reddi, başka koşudan parent reddi, zorunlu baseline adı, üç SQL girişinin gerçek plan kilidini beklemesi, atomik belge çifti, `0.0` sayısal kimlik, hash-only receipt, stop/terminal run sonrasında değişmez tekrar ve stop sonrası abandoned kaydı kontrol edildi. Oluşturulan UUID fixture'ları temizlendi.

Her iki kontrolde de `source_unchanged=true`. Migration SHA-256 `58e317af92df4b5a1ed0d4af5c72b459f0bd3405f47f8f16325a957ffdde6f8d`; schema SHA-256 `db33772e73131a54a8ef5115bdfda24abb74c6a336cb33940a25129e5d844463`. İncelenen kaynaklar JSON kayıtlarında bulunur.

**Sınır:** Yaşam döngüsü belgeleri açıkça sentetik, değerlendirme yapılmamış test fixture'larıdır. Bu kontrol gerçek aday skoru, LLM, fiziksel blob yazımı/okuması, replay, holdout metrik gizliliği veya 20 deneylik Director kabulünü kanıtlamaz. Holdout rapor filtresinin mevcut ayrı kanıtı SQLite fixture kapsamındadır. Gerçek Docker→Scorer entegrasyonunun yeni preregistration sözleşmesiyle kontrolü ve tam kalite kapısı bu dilim için henüz tamamlanmamıştır. M0 kabul maddeleri topluca kapanmaz.

## Entegrasyonda bulunan erken tamamlanma hatası

38+32 kontrolleri tekil kayıt API'lerini doğruladıktan sonra, yeni kayıtların mevcut finalizer ile birleşimi ayrıca incelendi. [Gerçek PostgreSQL karşı örneği](review-evidence/experiment-finalization-before.json), bir kayıtlı öneri ve ona bağlı tek sentetik Planner `candidate_crash` sonucu kullandı. Aday veya model çalıştırılmadı.

`OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_experiment_finalization_before.py` → karşı örnek doğrulandı, exit 0, `source_unchanged=true`:

- Görev planı kapandıktan sonra geçerli terminal experiment/trajectory çifti yazma denemesi `P0001` ile reddedildi.
- Buna rağmen üretim `IndependentScorer.finalize_if_ready()` rapor yayımladı: **run=completed, experiment=proposed, experiment record=0, trajectory record=0**.
- Yalnız teste ait UUID satırları silindi. Kamu verisi, Docker, GPU veya AOS kullanılmadı.

Bu bir başarı testi değildir; tamamlanma koşulundaki eksikliğin kanıtıdır. Düzeltme hem raporu bütün kayıtlı deneylerin terminal belge çiftleri tamamlanana kadar bekletmeli, hem de plan kapanışından sonraki geçerli terminal kayıt/kurtarma yolunu mümkün kılmalı. Sonrasında Docker→Scorer→terminal belge çifti→rapor zinciri ve stop/kurtarma bağlantısı yeniden doğrulanacak.

### 0012 düzeltmesi ve gerçek PostgreSQL regresyonu

`0012_experiment_report_fence`, raporun yayımlanmasını bütün kayıtlı deneylerin terminal durumda olmasına ve experiment/trajectory belge çiftlerinin bulunmasına bağlar. Scorer yalnız boolean sonuç veren ayrı yetkili SQL fonksiyonunu kullanır; tam belge tablolarına okuma yetkisi kazanmaz. Run durumunu terminale değiştiren doğrudan SQL de aynı koşulu denetler. Kapatılmış görev planına yeni öneri veya terminal olmayan durum geçişi eklenemez; geçerli atomik terminal belge çifti hâlâ yazılabilir. Stop sonrasında yalnız `abandoned` kaydı kabul edilir.

Migration öncesi bu projeye ayrılmış veritabanında run ve aktif iş sayısı sıfırdı. `alembic upgrade head` ve `alembic check` exit 0 verdi. Taslak revision adı Alembic'in 32 karakterlik standart sütununu aştığından uygulanmadan önce kısaltıldı. Downgrade'ın lifecycle guard gövdesi 0011 ile aynı olduğu kaynak karşılaştırmasıyla doğrulandı; gerçek downgrade çalıştırılmadı.

`OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_experiment_finalization.py` → **18/18, exit 0**, `source_unchanged=true`. [Sonuç kaydı](review-evidence/experiment-finalization-review.json):

- Eksik terminal belgeleri hem otomatik hem açık finalizer çağrısını durdurdu; rapor oluşmadı ve run `running` kaldı. Doğrudan Scorer SQL güncellemesi de reddedildi.
- Plan kapandıktan sonra geçerli terminal belge çifti yazıldı. Görev atanmamış ikinci kayıtlı öneri de kendi terminal belgeleri tamamlanana kadar raporu bekletti.
- Stop ve plan kapanışından sonra `abandoned` çifti yazılabildi; ardından `stopped` raporu üretildi. Tekrarlanan belge ve rapor çağrıları aynı sonucu verdi.
- Teste ait bütün UUID satırları temizlendi.

Scorer SHA-256 `18403ed5d60ef19edd4278ff63475278e9fa15b2ad1ea7dec49b86be383649a9`; 0012 migration SHA-256 `731d1a9eec1925865a9e6f471dd599c107f030bb0abf75f29378aa863f41adad`. Bu kontrolde gerçek PostgreSQL rolleri ve üretim Planner/Scorer servisleri, sentetik crash/cancel sonuçları ve açıkça test için hazırlanmış belgeler kullanıldı. Aday, metrik, fiziksel blob, LLM, kamu verisi veya AOS çalıştırılmadı. Güncellenmiş Docker/Scorer entegrasyonu ve tam kalite kapısı ayrıca gerekir; 20 deneylik Director kabulü açık kalır.

### Güncellenmiş Docker ve bağımsız Scorer zinciri

`uv run --python 3.12 pytest -q -m live tests/test_postgres_scorer_integration.py` → **1 passed, exit 0, 12.74 sn**. [Kanıt](evidence/postgres-scorer-process-latest.json), 2026-09-24 15:50:41 UTC koşusuna aittir; root 24 kaynak dosyanın SHA-256 değerini sonrasında karşılaştırdı ve değişiklik bulmadı.

Gerçek aday kaynağının hash'i ölçümden önce öneriye bağlandı. Ayrı fit/score Docker konteynerleri ve hardcoding/determinism/causality guard'larından sonra sayısal çıktı Planner kuyruğuna girdi; ayrı systemd Scorer süreci gerçek VUS metriklerini sentetik etiketlerle hesapladı. Director yalnız dev view'dan ölçülmüş sonuçları okudu. Plan kapandığında terminal deney belgeleri henüz bulunmadığı için ayrı finalizer `not_ready`, rapor API'si 404 verdi. Belgeler kapalı plan altında yazıldıktan sonra yeni finalizer hash ile doğrulanmış raporu yayımladı.

Bu tek adaylı entegrasyon baseline veya Referee iyileşmesi ölçmez; terminal karar açıkça `REJECT/baseline_not_measured` olarak saklandı. Genel code/messages/experiment/trajectory blob deposuna fiziksel belge yazımı yapılmadı; yalnız mevcut score artifact yolu kullanıldı. Test, tam Director döngüsü, 20 deney, kamu verisi, yerel LLM veya AOS kabulü değildir. İlk iki denemede test kodundaki dev-view `sample_count` türü ve image-digest biçimi varsayımları düzeltildi; son koşu gerçek exit 0 ile tamamlandı.

### Dilimin kalite kapısı

Son `scripts/quality_gate.py` kaydı 2026-09-24 15:52:22 UTC: **overall exit 0**, pytest **127 passed / 11 deselected**, strict mypy **47 kaynak**, Pylint **9.31/10**; Ruff, Pylint, Bandit, pytest, mypy, wheel build ve wheel import adımlarının her biri exit 0. Tam stdout/stderr ve komutlar [kalite kaydında](evidence/quality-gate-latest.json) bulunur. Önceki başarısız gate'te fingerprint testinin geçici kaynak ağacı yeni Director kapsamını içermiyordu; fixture yeni trusted dizine göre güncellendi ve Director dosyası mutasyon kontrolüne eklendi. Güvenilir üretim dosyalarını dışarıda bırakarak gate geçirilmedi.

Root son gate sonrasında hem live entegrasyonun 24 hem finalization regresyonunun beş kaynak SHA-256 değerini yeniden karşılaştırdı: değişiklik yok. Harness sözleşme sürümü `0.9.0`; Director ve DB kodu fingerprint kapsamındadır. Bu kapı CPU/statik/paketleme testlerini doğrular; gerçek GPU/AOS testleri dışarıda kalır ve M0 kabul tablosunda yeni bir tam kabul maddesi kapatmaz.
