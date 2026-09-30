# Scorer çalıştırma kimliği ve iptal toparlanması

Tarih: 2026-09-24. Başlangıç commit'i `d0ee384`, harness `0.6.0`. Önceki [yürütme incelemesi](10-execution-recovery-review.md), DB'de skor/terminal sonuç ayrımını ve sınırlı hata yeniden denemesini doğruladı. Bu dilim, `stop_requested` sırasında sahibi kaybolan veya hâlâ çalışan Scorer işlerinin güvenli kapanışını ele alır. Tam Director veya AOS/GPU birlikte çalışma kabulü değildir.

## Ölçülen host davranışı

[`review_systemd_invocation.py`](review-evidence/review_systemd_invocation.py), yalnız incelemeye ait küçük systemd servislerinde [10/10 kontrolü](review-evidence/systemd-invocation-capability.json) geçti. Her servis 64 MiB RAM, swap 0, %25 CPU, 8 PID ve 15 saniye çalışma sınırı altında `sleep` çalıştırdı.

- Aynı unit adı durdurulup yeniden başlatıldığında cgroup **yolu aynı**, `InvocationID` farklı oldu. Sadece unit adı veya cgroup yolu bir çalıştırma neslini tanımlamıyor.
- Aktif unit'in `MainPID` değeri gerçek `/proc/<pid>/cgroup` kaydıyla eşleşti; çalıştırma kimliği 32 haneli hex idi.
- Gerçekten bulunmayan unit, başarılı sorgu (`exit 0`) ile `LoadState=not-found`, `ActiveState=inactive`, `MainPID=0` ve boş kimlik/cgroup döndürdü.
- Servis yöneticisine erişilemeyen çocuk komut ortamı `exit 1` ve boş property kümesi döndürdü. Bu durum servis yokluğu kanıtı değildir. Hata üretmek için hem `DBUS_SESSION_BUS_ADDRESS` hem `XDG_RUNTIME_DIR` erişilemez yollara yöneltildi; yalnız ilkini değiştirmek bu host'ta bağlantıyı kesmedi.
- Ayrı sentinel servis diğerinin durdurulup başlatılmasından etkilenmedi. Sonunda yalnız bize ait iki servis durduruldu; ikisi de inactive, MainPID=0 ve cgroup'ları boş/kaldırılmış olarak doğrulandı.

Bu kanıt systemd kabiliyetini gösterir. Üretim Scorer, PostgreSQL claim, recovery writer veya AOS burada çalıştırılmadı.

## Uygulama ve bağımsız inceleme sınırları

Luna'nın `0009` taslağı, kuyruk claim'ini UUID'den türetilmiş unit adı ve gerçek çalıştırma kimliğine bağlar. Üretim kabulü için aşağıdaki noktalar ayrıca gerçek süreç/DB deneyleriyle doğrulanmalıdır:

1. Worker, claim almadan önce kendisinin beklenen unit/cgroup içinde bulunduğunu ve systemd kimliğinin eşleştiğini doğrular. Eksik veya sahte bağlamla claim alınmaz.
2. Yeni skor/terminal yazımı güncel token, lease ve çalıştırma kimliğiyle sınırlıdır. Alanları boş bırakmak mevcut görevin kuyruk satırını atlatmaz. Eski nesil yeni sahibin işini kapatamaz.
3. Stop/recovery, yalnız kayıtlı unit ve çalıştırma kimliğini hedefler. Erişim hatası, eksik property veya farklı çalıştırma kimliği durdurma/başarı yoluna dönüşmez.
4. Aynı unit'i başlatma ile inspect→stop aralığı ortak yaşam döngüsü kilidi veya eşdeğer bir sınırla korunur. İnceleme anından sonra başlayan yeni nesil durdurulmamalıdır.
5. Lease süresinin bitmesi tek başına süreç kapanışı değildir. Aktif cgroup'un bütün çocukları boşaltılmalı; ardından aynı claim nesli için DB iptali yapılmalıdır.
6. Normal skor, terminal sonuç, claim, stop ve recovery yolları aynı `run → job` kilit sırasını izler. SQL NULL değerleri eksik kimlikleri geçerli hale getirmemelidir. Planner'ın iş kuyruğuna alınmamış guard reddi ve queued iptal yetkileri korunur.
7. Tekrar edilen aynı kapanış, değişmez mevcut sonucu okuyarak devam edebilir; farklı sonuç veya eski yetki yeni kayıt üretemez. Rapor yayımlanması, araştırmanın başarılı sonuç bulduğu anlamına gelmez.

Bu liste doğrulanmış kabul iddiası değildir. Gerçek helper, migration ve süreç testleri hazır oldukça sonuçları aşağıya eklenir; 20 deneylik Director akışı, public veri, yerel model ve AOS/GPU kabulleri açık kalır.

## Gerçek worker kimliği ve durdurma kontrolleri

Üretim `verify_systemd_invocation` işlevi, altı ayrı gerçek systemd servisinde [6/6 senaryoyu](review-evidence/scorer-invocation-identity-review.json) geçti. Doğru MainPID, cgroup, kaynak slice'ı ve InvocationID kabul edildi. Farklı beklenen unit, sahte kimlik, eksik kimlik, erişilemeyen servis yöneticisi ve doğru isimli servisin yanlış slice altında başlatılması reddedildi. Bu deney credential veya veritabanı kullanmadı. Her servis en fazla 64 MiB RAM, %25 CPU, 8 PID ve 10 saniye ile sınırlıydı.

Üretim `stop_owned_scorer_unit` işlevinin [12/12 kontrolü](review-evidence/scorer-unit-drain-review.json), gerçek ana/çocuk süreçleri ve ayrı bir sentinel üzerinden geçti:

- Yanlış InvocationID ve servis yöneticisi erişim hatası süreçleri durdurmadan reddedildi.
- Durdurma çağrısının gerçek `flock` üzerinde beklediği `/proc/locks` ile görüldü. Başlatma/durdurma kilidi tutulurken süreçler yaşamaya devam etti.
- Doğru kimlikle durdurma, SIGTERM'i görmezden gelen ana süreci ve çocuğunu kapattı; ayrı sentinel etkilenmedi.
- Kasıtlı olarak `KillMode=process` ile başlatılan olumsuz örnekte `MainPID=0` olmasına rağmen çocuk yaşamaya devam etti. Helper kapanışın tamamlandığını bildirmedi. Test, kalan çocuğu kaydedilmiş başlangıç zamanı ve cgroup kimliğiyle doğrulanmış PIDFD üzerinden temizledi.
- Yalnız denemeye ait süreçler ve geçici dosyalar temizlendi; kaynak dosyaları test boyunca değişmedi.

Komutlar: `.venv/bin/python docs/ai-scientist/review-evidence/review_scorer_invocation_identity.py` ve `.venv/bin/python docs/ai-scientist/review-evidence/review_scorer_unit_drain.py`; son çalıştırmaların ikisi de exit 0. İlk drain denemesi test temizleyicisinde başarısız oldu: uv Python derlemesi `os.pidfd_open` ve `signal.pidfd_send_signal` sağlamıyordu. Host libc işlevlerinin varlığı ölçüldü ve test bunları kullanacak şekilde düzeltildi. Bu ilk hata üretim helper hatası olarak sayılmadı.

Bu sonuçlar kuyruk lease'i bittikten sonra DB iptalini, Director akışını veya AOS/GPU birlikte çalışmasını henüz kanıtlamaz. Testler yalnız JSON dosyalarında hash'leri belirtilen kaynak sürümleri için geçerlidir.

## PostgreSQL migration ve claim sınırları

`0009_scorer_invocation_fence` uygulanmadan önce gerçek DB'de eski `running` claim sayısı **0** olarak ölçüldü ([preflight](review-evidence/scorer-invocation-upgrade-preflight.json)). Migration da bu koşulu denetler; eski çalışan işe tahmini systemd kimliği atamaz. İlk upgrade denemesi PL/pgSQL `CASE` sözdizimi hatasıyla tümüyle geri alındı ve revision `0008` kaldı. İfade düzeltildikten sonra upgrade başarılı oldu.

`.venv/bin/python docs/ai-scientist/review-evidence/review_scorer_invocation_fence.py` gerçek servis rolleriyle [32/32 kontrolü](review-evidence/scorer-invocation-fence-review.json) geçti, exit 0. Kontroller:

- Eksik token, lease, unit veya InvocationID claim alamadı; yanlış unit reddedildi.
- Eksik/yanlış token, süreç kimliği, görev kimliği veya aday/çıktı hash'i skor yazamadı. Lease'i doğal olarak biten ve sonra yeniden alınan claim'in eski nesli de reddedildi.
- Geçerli skor ve terminal sonuç, ilgili işi aynı transaction içinde bitirdi ve geçici claim alanlarını temizledi. Sonuç kaydı süreç kimliğini korudu; gizli claim token'ı saklamadı.
- Planner'ın guard reddi ve sıradaki işi iptal yetkisi korundu. Aktif Scorer claim'ini Planner kapatamadı. `stop_requested` durumunda yeni skor reddedildi; geçerli Scorer iptali kabul edildi.
- Kalıcı sonuç olmadan çalışan işi bitirme ve aynı görev için hem terminal hem skor yazma girişimleri reddedildi.

Bu DB deneyinde yalnız bize ait 16 satırlık sentetik veri ve sentetik InvocationID'ler kullanıldı; systemd bağlamı ayrı süreç testinde sınandı. Deneme boyunca kaynak hash'leri değişmedi ve tüm fixture satırları temizlendi. Bu incelemenin ilk sürümünde expired-claim recovery açık kalmıştı; aşağıdaki `0010` gerçek süreç/DB kanıtı o dar recovery kabulini kapatır.

## Bu dilimin kalite kapısı

Güvenilir Scorer yolu değiştiği için harness sürümü önce `0.7.0`, `0010` recovery contract ile **`0.8.0`** oldu. `0.7.0` sürümünde guarded sandbox → kuyruk → ayrı systemd Scorer entegrasyonu **1 passed / 10.32 s**, Alembic şema uyumu exit 0 idi; bu sürüm geçmiş gate'tir. Son [tam kalite kapısı](evidence/quality-gate-latest.json) `0.8.0` kaynak ağacında exit 0: Ruff ve Bandit 0, Pylint 9.35/10, pytest 120 passed / 11 deselected, strict mypy 40 dosyada başarılı, wheel build/import 0. Önceki sürümlerde ölçülen geniş guard ve public-data sonuçları bu sürümde yeniden ölçülmüş gibi sunulmaz.

## `0010` stop-requested expired-claim recovery

`0010_terminal_recovery_fence` eski worker InvocationID'sini korurken yeni recovery InvocationID'sini ayrı saklar. Recovery, job lifecycle kilidini eski unit/cgroup drain edilip aynı canonical systemd unit altında yeni generation doğrulanana kadar tutar. Yalnız stop-requested run'ın önceki claim token'ı ve invocation snapshot'ı halen DB'de eşleşiyorsa cancelled terminal outcome yazılır. Sonuç INSERT'i ve stopped report finalization ayrıdır; tekrar çağrı aynı immutable sonucu ve rapor hash'ini okur. Recovery sadece recovery generation farklı ve gerçekten doğrulanmışsa başarı döner.

Güncel [gerçek süreç + PostgreSQL kanıtı](review-evidence/scorer-stop-recovery-review.json) **23/23** kontrol geçti, `source_unchanged=true`, `all_passed=true`. Kaynak SHA'ları bu JSON'da worker, supervisor, service, jobs, schema, CLI, Planner task-plan ve migration `0010` için sabitlenmiştir. Kontrollerde:

- Claim doğal olarak lease süresini aşarken worker canlı kaldı; API stop talebi tek başına işi iptal etmedi.
- Yanlış recovery generation reddedildi ve eski worker çalışır bırakıldı. Doğru generation exact eski systemd unit/cgroup'u boşalttı, sonra yeni InvocationID ile fenced cancel yazdı.
- Dispatcher, başka bir running run'da queued iş varken de stop-requested canlı owner recovery'sini seçti. İşin ardından competing queued task yanlış claim edilmeden sırada kaldı.
- Ayrı recovery finalizer sahte skor oluşturmadan doğrulanmış stopped report yazdı; aynı recovery tekrarı sonuç ve report digest'ini değiştirmedi.
- Recovery GPU admission slotunu serbest bıraktıktan sonra farklı run'ın gerçek worker'ı VUS metriği hesaplayıp completed report yayımladı. Ayrı sentinel canlı kaldı; yalnız fixture süreçleri ve satırları temizlendi.

Bu kanıt sentetik candidate/task fixture'ı ve küçük bounded CPU işidir; 20 deneylik Director, tüm verdict/crash senaryoları, public-data/replay/holdout, gerçek yerel GPU/LLM, AOS birlikte çalışma ve forbidden-access dış gözlem kabulü açık kalır. Dispatcher seçim testi tek başına fairness ya da sürekli koşuda starvation olmadığını kanıtlamaz.
