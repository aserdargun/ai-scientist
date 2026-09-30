# Public süit ve sınırlı onarım entegrasyonu

Tarih: 2026-09-25. Başlangıç commit'i `3e98269` / harness 0.23.0.
Bu kayıt 0.24 entegrasyonunun kanıtlarını ve açık kabul işlerini ayırır.

## Gerçek public süit

Luna'nın sabitlenmiş loader/suite dilimi ana koda alındı. Ana Director
replay/recovery düzeltmeleri korundu; ayrı holdout schema taslağı bu
birleştirmeye dahil edilmedi. Kaynak başlangıç/son hash'leri
`review-evidence/public-suite-024-main-source.json`, teslim kapsamı
`public-suite-source-delta.json` içindedir.

Varsayılan süit **27 görev** içerir: CARE farm A'dan 12 PDM + 10 NRM,
SKAB'dan bir EVT, SMD sunucu telemetrisinden bir EVT, Genesis/GECCO/CATSv2'den
üç EVT. Yedi bağımsız aile vardır. CATSv2 simülasyon ve bronze olarak
işaretlenir; SMD endüstriyel kaynak olarak sunulmaz. Ticari olmayan
araştırma profili ve kaynak atıfları korunur; GHL/SWaT dahil değildir.

TSB üyeleri sabit yollarla seçilir; upstream train prefix'i, yalnız train'den
hesaplanan pencere ve embargo, etiket değerlerinden bağımsızdır. İlk ölçümde
64 MiB JSON sınırı için son 5000 örnek kullanıldı; aşağıdaki başarısız
uygunluk kontrolünden sonra v2 tam eval politikasına geçildi. Genesis'in fiziksel kadansı doğrulanmadığından
`sampling_s=None` yalnız EVT için kabul edilir. GECCO'nun kayıp satırları ve
saat geri dönüşü maske ile korunur. Zaman politikası
`review-evidence/tsb-source-time-axis-adr.md` içindedir.

SKAB valve1/0 seçimi sabittir: düzenli ızgaranın ilk %60'ındaki en uzun
kesintisiz gözlem dilimi train olur. Bu üye **26 train satırı ve pencere 10**
verir; eval 470 satırdır. Mevcut boyut koşulu train ≥ pencere+1, eval ≥
penceredir. Bu küçük train diliminden geniş bir istatistiksel yeterlilik
sonucu çıkarılmamıştır; seçim veya pencere etiketlere bakılarak değiştirilmez.

Luna'nın izole kabulü ve ana kodun gerçek tekrarı aynı manifesti üretti:

- Ana tekrar: **session 30738 / exit 0**, **13,485 saniye**.
- Manifest: **44.758.674 bayt**, yüklenen matrisler **31.928.120 bayt**;
  süreç RSS zirvesi **676.233.216 bayt**.
- SHA-256: `7a7ba9fad34c36227d125c9a93c45c0aeb940841e8dd35cbba408e4cfe3ecbef`.
- Gerçek kayıt, yalnız özel test DB'sinin trusted migrator rolünden
  yapıldı; ayrı Planner rolünden hash/kimlik/aile/ağırlık okuması geçti.
- 2 GiB RAM, swap 0, bir CPU, 240 saniye sınırı; ortak CPU test kilidi.
  Kaynaklar komut boyunca değişmedi. Canlı/main DB'ye yazılmadı.

Kanıt: `default-public-suite-024-main-check.json` ve
`default-public-suite-024-main-command.json`. Manifest yalnız özel runtime
dizininde tutulur; etiketler manifestte bulunmaz. Bu kontrol public veriyle
model başarısı veya bütün araştırma döngüsü kabulü değildir.

## İncelemede bulunan eksikler

Donmuş manifestin salt okunur EVT uygunluk kontrolü ek bir sınır gösterdi:
GECCO ve CATSv2'nin son 5000 örnekli görünümünde, maskeler çıkarıldıktan
sonra iki etiket sınıfı birlikte bulunmuyor. SKAB/SMD/Genesis bu koşulu ve
sabit pencere için örnek sayısı koşulunu sağlıyor. Kanıt
`public-evt-024-eligibility.json`; kontrol ham etiket, indeks veya puan
yayınlamaz ve manifesti değiştirmedi. Komut teknik olarak exit 0 verdi,
ancak kabul sonucu `passed=false` olduğundan **EVT uygunluğu geçmedi**.

Etiketlere göre yeni pencere aranmayacak. Tam source-prefix/embargo sonrası
eval bölümünü taşıyabilmek için JSON boyut sınırı ve kaynak bütçesi yeniden
ölçülecek; matris sınırı korunacak. Mevcut 27 görevlik kayıt/okuma başarısı,
bu iki görevin puanlanabildiğini kanıtlamaz.

Tam TSB eval bölümleriyle ayrı, sınırlı kapasite denemesi **session 25895 /
exit 0**, **11,986 saniye** verdi. Matrisler **52.126.816 bayt** ile mevcut
64 MiB sınırında kaldı; JSON **72.329.043 bayt**, RSS zirvesi
**822.964.224 bayt** oldu. Bu tam bölümlerde beş EVT görevinin tamamı
iki sınıf ve sabit pencere için yeterli örnek koşulunu sağlıyor. İnceleme
süreci JSON sınırını yalnız kendi belleğinde 128 MiB yaptı; ürün sınırı veya
DB değiştirilmedi. Üründe ortak API/Director 96 MiB JSON sınırı ve aynı
64 MiB matris sınırı, tam TSB eval ve süit sürümü 2 değişikliği birleştirildi.
TSB kaynak/split/profil kimlikleri v2 politikasını içerir; teslim kaydı
`public-capacity-024-main-source.json` içindedir.
Kanıt: `public-full-eval-capacity-review.json` ve
`public-full-eval-capacity-command.json`. Bu kaynak ölçümü henüz Scorer
çalışma süresi veya model/araştırma sonucu değildir.

Önceki EVT Scorer yolu trusted maskeyi uygulamıyordu. Dar düzeltme ana koda
alındı: trusted eksen/maske/digest önce doğrulanır, aynı örnekler skor ve
etiketten birlikte çıkarılır, train'den türeyen pencere sabit kalır.
Kaynak saat sıçraması yalnız trusted maskenin ayırdığı segment sınırında
kabul edilir. PDM/NRM, alarm policy ve commit öncesi yeniden sahiplik/
semantics kontrolleri korunur. Eski worktree uyumluluk şimleri alınmadı.

Luna'nın private PostgreSQL → Director kaydı → Planner planı → gerçek
sınırlı Scorer worker kanıtı **6/6, exit 0**; sekiz satırdan maskeli biri
çıkarılır, kalan yedi satırın VUS sonucu birebir eşleşir. 16 hedefli test,
Ruff ve iki dosyalık mypy exit 0. Kaynak eşliği
`masked-evt-024-main-source.json`, worker kanıtı
`holdout-masked-evt-scorer-review.json` içindedir. Orijinal sürücü
`review_masked_evt_private_original.py` kaynak arşividir; yalnız kayıtlı
izole recovery worktree/DB bağlamında çalıştırılmıştır.

Ana kodda gerçek v2 public Scorer kayıtlarıyla ek salt okunur kontrol
exit 0 verdi: beş EVT profili doğrulandı; GECCO 123.301 satır/1045 maske,
SKAB 470 satır/20 maske, SMD 12.000 ve CATSv2 83.332 satır/maske yok.
Genesis fiziksel kadansı bilinmeyen sample-index EVT olarak kalır.
`public-evt-semantics-024-review.json` bu sonucu helper ve manifest
hash'lerine bağlar. Bu kontrol skor hesaplamaz; tam public Scorer ve
holdout yaşam döngüsü kabulü hâlâ açıktır.

0.23 gerçek Qwen bölümü ilk yanıtı tamamladıktan sonra onarım admission'ında
durmuştu. Gerçek pinned tokenizer ile onarım istemi seçimi ana koda alındı:
tam eski yanıt sığıyorsa korunur; sığmıyorsa görev/şampiyon bağlamı ve kapalı
hata koduyla yeni tam öneri istenir. Yalnız en eski isteğe bağlı feedback
çıkarılabilir; kaynak kod kesilmez. Kalıcı tamamlanmış/başlatılmış onarım
receipt'i kullanılırken tokenizer veya model yeniden açılmaz. Tek onarım,
6000 karakter kaynak ve mevcut toplam token/süre sınırları korunur.
20 hedefli test ve gerçek CPU tokenizer kontrolü geçti: sentetik uzun
yanıtta tam onarım 3353 token, kompakt onarım 645 token. Bu ölçüm önceki
başarısız GPU yanıtının birebir tekrarı değildir.
Kanıt `local-qwen-repair-source-freeze.json`, `repair-context-tokenizer-check.json`.
Ana kodun tam imaj/kalite kapısı aşağıda ayrıca doğrulanmıştır;
0.23 kapısı yeni kaynaklara aktarılmamıştır.

Tam public görev kartlarının bağlam ölçümü de yapıldı: production
`DirectorLoop._task_cards` ve gerçek pinned tokenizer, 27 görev ve robust-z
kaynağı için **4259 token** saydı (**session 2120 / exit 0**, 4,873 sn).
Henüz ölçülmemiş skor alanları açıkça sıfır placeholder'dır, feedback yoktur;
bu bir araştırma sonucu değildir. Mevcut 2048 tokenlık smoke profili bu
bağlamı alamaz. Görev kartları/şampiyon kesilmeden daha geniş araştırma
profilinin gerçek GPU kapasitesi ölçülmelidir. Kanıt
`public-prompt-capacity-review.json` ve `public-prompt-capacity-command.json`;
ölçüm CPU üzerinde yapıldı ve model başlatmadı.

Tam v2 manifestinin ilk gerçek DB kaydı **session 23185 / exit 1** verdi:
GECCO'nun 123.301 etiketi tek sorguda PostgreSQL'in 65.535 parametre
sınırını aştı. Kanıt `default-public-suite-v2-024-main-command.json`;
başarısızlık kaynak boyut ölçümünü veya ilk kısaltılmış süitin kaydını
geçersiz kılmaz, fakat tam v2 kayıt kabulünü açık bırakır. Etiketler görev
transaction'ı içinde küçük partilere ayrılacak; önce tamamlanan kayıtlar
silinmeden idempotent tekrar yapılacak. Ham SQL/etiket içeren hata logu
özel runtime alanındadır; review JSON'a taşınmaz.

10.000 satırlık SQLAlchemy executemany partileri mevcut görev transaction'ı
içinde uygulandı. **Session 25825 / exit 0**, **28,207 saniye** tekrarında
27 görevin tamamı kaydedildi, ayrı Planner rolünden okundu ve gerçek API
SuiteRegistry dosya/hash doğrulaması geçti. Önceki kayıtlar silinmedi;
idempotent yol kullanıldı. Süit sürümü 2, JSON **72.329.043 bayt**, matris
**52.126.816 bayt**, süreç RSS zirvesi **971.333.632 bayt**.
Manifest SHA-256 `4aeeb8c015ebb7f3ef68a76d38b158a7d286f0b9a46b6fa19a8682c3a4a8ca1f`.
V2 profil kimlikleri nedeniyle önceki kapasite probe'unun manifest hash'i
bu son manifest için kullanılmaz. Kanıtlar
`default-public-suite-v2-024-batched-check.json` ve
`default-public-suite-v2-024-batched-command.json`; kaynaklar sabittir.
Bu kayıt/okuma kabulü aday başarısı veya public Scorer kabulü değildir.

AOS bootstrap/restart/takeover dilimi ayrı public kaynak kopyasında hazır.
`aos-lab-optin-lifecycle.patch` ve review JSON'u dokuz kaynak dosyasına
bağlıdır; root base/head eşliğini ve patch uygulama kontrolünü doğruladı.
24 hedefli test ve 4100 paket kontrolü exit 0. Geniş testte eksik yerel
Chromium manifestinden 12 hata (26 geçti, 1 atlandı) ve tam Ruff'ta 206
önceden mevcut tanı açık kaydedildi. Canlı AOS değiştirilmedi; bu dilimde
model/GPU çalıştırılmadı.

Sonraki control-fence yaması `aos-lab-optin-control-fence.patch` de
bağımsız incelendi: policy await sonrası binding/typed state ve stop için
güncel HUMAN lease yeniden kontrol edilir, kısa deadline sonra başlar.
Status/report host policy ile çalışır; polling model açmaz. İki yama
private kaynak kopyasına sırayla uygulandı ve son üç dosya birebir eşleşti.
**27 hedefli/restart API testi ve 4100 paket kontrolü exit 0**; kanıt
`aos-lab-optin-control-fence-review.json` ve `...-root-review.json`.
Gerçek birlikte araştırma/desktop kabulü, holdout döngü bağlantısı ve
eğitim ölçümleri açıktır.

## Gerçek Qwen onarım denemesi

**Session 30449 / exit 1**, **251,188 saniye**: üretim provider'ı S2 öneri
ve tek onarım çağrısını gerçekten tamamladı. İstemler 561 ve 959 token,
çıktılar ayrı ayrı 870 token; bağlam admission hatası tekrar etmedi.
Ancak iki yanıtın hash'i aynıdır ve son kaynak 1094 karakterlik tek satırda
geçersiz Python içerir (AST satır 1, sütun 91). Ham kaynak ve düşünme metni
yayımlanmaz. Provider `tool_parse` ile kapandı; geçerli öneri veya araştırma
sonucu yoktur. 13 kontrolün 11'i bütçe/sahiplik/temizleme kanıtıdır; iki
sonuç kontrolü başarısızdır ve tüm deneme başarılı sayılmaz.

GPU zirvesi **12860 MiB**, son **62 MiB**; en düşük kullanılabilir RAM
**19.574.263.808 bayt**, sıcaklık en fazla **59°C**. İki ticket `done`, model
cgroup'ları boş, parent terminal; başka GPU süreci görülmedi. Model/runtime
ve kaydedilmiş provider kaynakları değişmedi. Kanıt
`local-qwen-provider-024-episode-review.json` ve sürücüsü
`review_local_qwen_provider_turn_v2.py`.

Bu bağımsız provider denemesi Docker aday yürütmesi veya Scorer çağırmadı;
imaj entegrasyonu tamamlanmadan çalıştırıldı. Son imaj/gate kaydı aşağıda
ayrıca bağlanmıştır. Çok satırlı geçerli kaynak üretimi ve daha geniş public
araştırma profili sonraki Luna diliminde incelenmektedir.

## Son kalite kapısı ve kaynak bağı

İlk birleşik kapı session 9250 / exit 0 ile 304 test geçti. Son kaynak
incelemesinde yeni `lab/suite_limits.py` modülü trusted harness hash'ine de
alındı; mutasyon testi bu baytların hash'i değiştirdiğini doğrular. Bu
somut değişiklikten sonra imaj ve zorunlu yedi komut yeniden çalıştırıldı:

- **Session 23650 / exit 0**: **305 passed, 7 skipped, 13 deselected**.
- Strict mypy **78 kaynak**, Pylint **9.35**; yedi komutun tamamı exit 0.
- İmaj **87 runtime dosyasında byte eşliği** verdi:
  `sha256:88c617d0082d9d862c830d49159c94df58804289dbc8efe8a9c96aecedc34683`.
- Harness **70 dosya**, SHA-256
  `7d8f157024790b7c5b4b47b66340a861bc80b5390e2c4f27ebba56aac1d6861e`.
- Final kalite komutları boyunca kaynaklar sabittir. İmaj hazırlığının
  wrapper kaydındaki source change yalnız pinlenen imaj kilidinin beklenen
  yenilenmesidir; image receipt'in kendi kaynak eşliği ayrıca geçmiştir.

`parallel-integration-024-quality-gate-binding.json` kaynak, imaj, tüm
komutların gerçek exit code'ları ve review artefaktlarını bağlar. Kalite
kapısı başarısı başarısız S2 araştırmasını veya açık M0 maddelerini kapatmaz.
