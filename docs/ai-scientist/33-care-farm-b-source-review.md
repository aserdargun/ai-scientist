# CARE Farm B kaynak incelemesi

Tarih: 2026-09-27. Kaynak doğrulaması ve ayrı `holdout-030` kopyasındaki
Farm B yükleyicisinin gerçek materyalizasyonu, ayrı PostgreSQL kurulumu ve
Scorer rolüyle geri okuması tamamlandı. M0.10 holdout skorlama kabulü
henüz tamamlanmış değildir. Yükleyici/adapter daha sonra ana
0.30 sürümüne alındı; kabul sayıları 10 geçti / 8 kısmi / 4 açık kalır.

## Kaynak ve ölçülen kapsam

Sabit kaynak [Zenodo v6, kayıt 15846963](https://zenodo.org/records/15846963).
Yerel arşiv 5.503.439.673 bayt; SHA-256
`ca61379e98956d891041ad45c885109bd8a14199fde0688d0184a11c2d4194f1`.
Luna/high kaynak taraması Farm B'nin 15 CSV üyesini sırayla açtı; arşivi
tam çıkarmadı ve Farm C veri üyelerini açmadı. Üye hash'leri, kaynak saatleri,
durum ve özellik sayaçları `review-evidence/care-farm-b-source-audit.json`
içindedir. `care-farm-b-policy-analysis.json`, kaynak taramasının SHA-256'sına
bağlı türetilmiş sonuçtur.

Arşiv taraması worker tarafından **exit 0**, 42,314 saniyede tamamlandı.
Root dört dosyanın son hash'lerini doğruladı ve türetilmiş politika analizini
yeniden çalıştırdı: **exit 0**, sonuç JSON'u aynı baytlarla üretildi.
`review-evidence/care-farm-b-root-review.json` bu bağı ve systemd kaydını
tutar. Arşiv taramasındaki yuvarlanmış 1G bellek zirvesi, 1 GiB cgroup
sınırı altındaki rapordur; kesin Python heap boyutu olarak yorumlanmaz.

| Ölçüm | Sonuç |
|---|---|
| Farm B görevleri | 15: 6 PDM, 9 NRM; tümü kapsamda |
| Tahmin dönemi | Toplam 72.128 satır; her görevde tam 600 saniyelik ızgara |
| Tahmin ızgarasında eksik zaman noktası | 0 |
| Eğitim ızgaralarında eksik zaman noktası | Toplam 1.462 |
| Kaynak sütunları | 257; bunların 63'ü ortalama sensör ölçümü |
| Normal durum ve bir günlük embargo sonrası en uzun kesintisiz eğitim bölümü | Görev başına 1.111–1.536 satır; henüz kabul edilmiş eğitim politikası değildir |
| Altı adlandırılmış fiziksel özellik, tam train + prediction ham float64 | 41.235.120 bayt |
| Tüm 63 ortalama özellik, tam train + prediction ham float64 | 432.968.760 bayt |

Bayt değerleri serileştirilmiş Arrow boyutu veya ölçülmüş bellek zirvesi değildir.
Altı özellik ve yalnız en uzun eğitim bölümü için taramada yer alan
4.399.104 baytlık başka senaryo, tam eğitim döneminin boyutuyla karıştırılmaz.

## Farm B'ye özgü kaynak yorumu

[Veri makalesi, v2 §3.2–3.3](https://arxiv.org/html/2404.10320v2), Farm B
durum kodlarını işletme modları ve servis kayıtlarıyla ilişkilendirir; eksik
değerlerin sıfırla değiştirildiğini ve durum kayıtlarında tutarsızlık
bulunabileceğini belirtir. Sıfır değerleri otomatik olarak eksik sayılmayacak;
durumlar ileri doldurulmayacak veya tahminle düzeltilmeyecek.

[Yazarların değerlendirme açıklaması](https://aefdi.github.io/EnergyFaultDetector/care2compare_faq.html#which-timestamps-are-used-for-pointwise-evaluation)
Farm B/C için normal olmayan durumların noktasal değerlendirmeden çıkarılmasını
tanımlar. Buna göre Farm B için önerilen maske, gözlenen 1/3/4/5 durumlarını
dışlar; 0/2 durumlarını korur. Bütün tahmin satırları ve zaman aralıkları
korunacak, maskeli satırlar diziden silinmeyecektir. Farm A'nın değerlendirme
politikası ayrı kalır. Bu yorum root tarafından birincil kaynaklarda da
kontrol edildi.

Altı adlandırılmış ortalama özellik politikası: `reactive_power_11_avg`,
`power_58_avg`, `wind_speed_59_avg`, `wind_speed_60_avg`, `wind_speed_61_avg`,
`power_62_avg`. Seçim kaynak şemasına dayanır; tahmin değerlerine, olay
etiketlerine veya model başarısına göre yapılmaz. Fiziksel özellikler aynı
Farm A seçimi ilkesiyle karşılaştırılabilir; kaynak sütun adları farklıdır.

Kaynak saatleri anonimleştirilmiş ve zaman dilimi içermiyor. Ölçülen 600 saniye
örnekleme korunur; bu saatler gerçek UTC ölçümü diye sunulmaz. Eğitim yalnız
kaynak train önekinden seçilir.

## Astra/high bulgusu: normal durum, sağlıklı süre değildir

Kaynak raporundaki `positive_healthy` ifadesi, olay penceresindeki normal
işletme durumuna sahip pozitif noktaları sayar. PDM yanlış alarm bütçesinin
gerektirdiği **olay dışındaki sağlıklı değerlendirme süresini** kanıtlamaz.
Özellikle görev 53'ün 6.048 prediction satırının tamamı olay penceresindedir;
bu görevde mevcut yorumla sağlıklı süre sıfır olur ve PDM metriği reddeder.
Diğer beş PDM görevindeki olay dışı maskelenmemiş satır sayıları sırasıyla
19: 841, 27: 288, 34: 843, 7: 701, 77: 361'dir.

Bu nedenle yalnız tam prediction dönemini kullanmak, 15 görevin tamamı
için kabul edilmiş scoring politikası değildir. İlk kaynak/politika JSON'ları
tarihsel tarama olarak korunur; bu inceleme onların sağlıklı süre yorumunu
düzeltir. Görev 53 çıkarılmayacak veya metrik hatası başarıya çevrilmeyecektir.

## Seçilen yerel uyarlama: sabit yedi günlük referans

Astra/high aşağıdaki politikayı **sürümlü yerel değerlendirme uyarlaması**
olarak uygun buldu. Aşağıdaki gerçek materyalizasyon bu politikayı uygular;
veritabanı ve skor kabulü ayrıca gereklidir.
CARE'in yayımlanan benchmark sonucuyla eşdeğer olduğu iddia edilmez.
Karar ve görev başına sağlıklı süre hesabı
`review-evidence/care-farm-b-astra-policy.json` içindedir.

1. Her 15 görevde, etiketlere bakmadan, prediction başlangıcından önceki tam
   yedi gün değerlendirmeye ayrılır. Kaynak train sonundaki bu bölüm model
   fit'i, eşik seçimi, özellik seçimi veya pencere hesabı için kullanılmaz.
2. Fit son sınırı bu referans bölümünün başlangıcından 86.400 saniye öncedir.
   Altı sabit özellikte sonlu, status 0/2 olan en uzun kesintisiz bölüm
   seçilir; eşitlikte en erken bölüm alınır. Otokorelasyon penceresi yalnız
   o bölümden hesaplanır. 10–100 örnek sınırında bir günlük embargo yeterlidir.
3. Referans ve orijinal prediction boyunca 600 saniyelik ızgara korunur.
   Eksik noktalar yalnız fit medyanlarıyla doldurulur ve tamamen maskelenir.
   Gözlenen sıfırlar korunur; durum kodları doldurulmaz. Olay zamanları
   değişmez, dizinleri eklenen referans kadar kayar.
4. Referansın nominal sağlıklı etiketi kaynak train rolü ve status 0/2
   bilgisine dayanır; bütün train verisinin kesin sağlıklı olduğu iddia
   edilmez. Normal olmayan durumlar yine maskelenir.
5. Hiçbir görevde süre, değerlendirme sonucuna göre uzatılmaz. Yeterli
   kesintisiz fit veya pozitif sağlıklı süre bulunmazsa yükleme reddedilir;
   görev sessizce atlanmaz.

Yeni ızgara toplamı **87.248** olur: 72.128 orijinal prediction noktası ve
15 × 1.008 = 15.120 referans noktası. Eklenen, kaynakta gözlenen ve eksik
referans noktaları ayrıca raporlanır. İlk kaynak taraması yeni cutoff'u
ölçmemişti; aşağıdaki yükleyici denemesi bu ölçümü ayrıca yaptı.

## Gerçek 15 görev materyalizasyonu ve Astra incelemesi

`review-evidence/care-b-prefix-handoff.json` kaynak hash'lerini ve worker'ın
gerçek komut sonuçlarını, `care-b-prefix-materialization.json` görev başına
ölçümleri taşır. Son materyalizasyon **exit 0**, unit
`swapp-care-farm-b-prefix-final.service`; 36,210 saniye duvar süresi ve
18,107 saniye CPU süresi. Root son kaynak/rapor hash'lerini ve unit journal
kaydını ayrıca doğruladı. Farm C veri üyeleri açılmadı.

| Ölçüm | Sonuç |
|---|---|
| Tam kapsam | 15 görev: 6 PDM, 9 NRM |
| Korunan özgün prediction satırları | 72.128 |
| Sabit referans ızgarası | 15.120 ek nokta |
| Toplam değerlendirme ızgarası | 87.248 nokta |
| Yeni cutoff sonrası fit | 1.111–1.536 kesintisiz satır |
| Yalnız fit'ten hesaplanan pencere | 38–100 örnek |
| Toplam gerçek Arrow serileştirmesi | 5.179.824 bayt; 2 GiB sınırının altında |
| En büyük Arrow girdisi | 727.640 bayt; 32 MiB sınırının altında |
| Python süreç RSS zirvesi | 210.200 KiB |
| systemd cgroup zirvesi | Yuvarlanmış 1G; sınır 1 GiB, swap kapalı |

Görev 53'ün referansında 1.007 kaynak noktası ve bir ızgara boşluğu var;
toplam beş maskeli noktadan sonra **1.003 sağlıklı, maskelenmemiş nokta**
kalıyor. Özgün 6.048 prediction noktası ve 5.711 maskelenmemiş pozitif
nokta korunuyor. Böylece bu görev çıkarılmadan sağlıklı süre koşulunu sağlıyor.

Astra/high üç dondurulmuş kaynak dosyasında entegrasyonu engelleyen hata
bulmadı. Sabit özellikler, fit ve pencerenin değerlendirme etiket/değerlerinden
bağımsızlığı; olay uçları, 600 saniyelik ızgara, status 0/2, eksik hücrelerin
fit medyanıyla doldurulması ve gözlenen sıfırların korunması incelendi.
Hedefli **10 test**, Ruff ve mypy başarılıdır. Bu kanıt veritabanına kayıt
veya Scorer çalıştırması içermez.

Materyalizasyon raporundaki `all_prediction_rows_mask_preserved` boolean'ı
yalnız pozitif grid sayısını denetler; bağımsız kaynak-maskesi eşliği kanıtı
olarak kullanılmaz. Arrow ölçümü labels/mask/times/context JSON kayıtlarının
boyutunu ölçmez. İlk adapter testi yalnız binding doğrulamasını çalıştırıyordu.

Dar R2 düzeltmesi **13 test**, Ruff ve mypy ile başarılıdır
(`review-evidence/care-b-prefix-r2-report.json`). Referans değerleri ve durum
kodu değiştirilince fit/pencere eşliğini koruyan fixture ve registration
builder'ın gerçek çağrısı eklendi. Builder testi labels/mask/times uzunlukları,
semantik digest, FitContext ve Arrow roundtrip'ini kontrol eder; gerçek DB
kaydı içermez. Kurulum için gereken ayrıcalıklı yetki doğru belgelendi.
Yeni rapor sürücüsü maske kontrolünü vektör uzunluğu/kaynak ızgarası eşliği
olarak adlandırır. İlk gerçek rapor ve handoff değiştirilmedi. Yükleyicinin
hash'i aynı olduğundan 15 görevlik arşiv taraması tekrarlanmadı.

## Gerçek PostgreSQL kayıt denemesinin durumu

İlk provanın snapshot'ında `ops/sandbox-image.lock` eksik olduğu için pytest
collection durdu; veri kaydı çalışmadı. Root sürücü bu dosyayı da snapshot'a
alacak şekilde düzeltildi. İkinci deneme **session 29522 / exit 1**:
15 görevin materyalizasyonu ve privileged profile/label/semantics kurulumu
sonrası `register_holdout_suite` yanlışlıkla Scorer rolüyle çağrıldı.
`SELECT ... FOR UPDATE` yetki reddi verdi. Adapter sözleşmesinde belirtildiği
gibi süit kurulumu migrator ile, runtime geri okuması Scorer ile yapılmalı.
Rol yetkileri veya immutable migration bu test için genişletilmiyor.

İkinci provada migration head 0021 ve **72 rol/olmayan kaynak kontrolü**
başarılı, fakat kayıt testi başarısızdır. Yalnız kendi konteyneri/kimlik
bilgileri temizlendi. Kanıtlar `care-b-registration-pg-a4855d1c2a5c.json`
ve `care-b-registration-pg-e0f999d9623d.json` içinde korunur.

Astra/high ayrıca testte herhangi bir DB hatasının erişim reddi sayılmasını
hatalı buldu; SQLSTATE 42501 doğrulanmalı ve yalnız doğrulanan retler
sayılmalı. Weight/normalization ve suite header geri okuma kontrolleri de
aynı test düzeltmesine ekleniyor. Bunlar registration fixture düzeltmeleridir;
gerçek baseline kalibrasyonu veya Scorer değerlendirmesi değildir.

## Gerçek kayıt ve Scorer geri okuma sonucu

Düzeltilen son test **session 89405 / exit 0**, 1 canlı pytest testi ve
**72/72** ek rol/olmayan kaynak kontrolüyle tamamlandı. Süit profile,
label, semantics ve registry kurulumu yetkili migrator rolüyle yapıldı;
tüm geri okuma ayrı authenticated Scorer rolüyle çalıştı. Director ve Planner
üç private tablo için toplam **6/6 SQLSTATE 42501** reddi aldı.

15 görev (6 PDM / 9 NRM) ve **87.248** değerlendirme noktası için labels,
maskeler, zamanlar, failure windows, semantik digest, FitContext, Arrow
baytları, matris değerleri/sütunları, ağırlık ve normalization değerleri
SQL geri okumasıyla eşleşti. Süit manifesti, development manifest bağı,
görev sayısı ve epsilon da doğrulandı. Normalization **sentetik 0/1**
kurulum değeridir; ölçülmüş baseline kalibrasyonu değildir.

| Saklanan içerik | Toplam canonical payload baytı |
|---|---:|
| Etiket JSON | 491.415 |
| Maske JSON | 516.138 |
| Zaman JSON | 1.919.471 |
| FitContext JSON | 2.910 |
| 30 Arrow blob | 5.179.824 |

Bu boyutlar PostgreSQL fiziksel disk/TOAST kullanımı değildir. En büyük
Arrow blob 727.640 bayttır. Servis 43,554 saniye duvar / 21,053 CPU saniyesi,
yuvarlanmış 1G cgroup zirvesi ve sıfır swap ile çalıştı. 1 GiB bellek,
yüzde 50 CPU ve 64 task sınırı korundu; ayrıca PostgreSQL konteyneri
512 MiB / 0,5 CPU ile sınırlandırıldı. Tüm host için birlikte çalışma
performansı ölçülmüş sayılmaz.

Kaynak ve snapshot değişmedi; yalnız bu provanın konteyneri ve DB kimlik
bilgileri temizlendi. Çıktı yalnız toplamlar ve hash'ler içerir:
`review-evidence/care-b-registration-readback.json`.
Komut/kaynak/temizlik bağı
`review-evidence/care-b-registration-pg-045ed75c64bb.json`;
tam çalıştırılan test hash'i
`db4f548e1ea258e70da79b26c0ff520fa46126b545151749ff93912cfadb3846`.

Önceki üçüncü denemede (session 73218 / exit 1) kurulum geçmişti;
testte kaynak görev ID'si yerine SQL hash anahtarıyla binding aranması
`KeyError` üretti. Astra bunu kaynak incelemesinde de yakaladı.
`care-b-registration-pg-7aed2d23fb05.json` başarısız kanıt olarak korunur;
son deneme kaynak görev ID'siyle karşılaştırır. İlk iki başarısız deneme
yukarıda kayıtlıdır. Hiçbiri başarıya dönüştürülmedi.

## Holdout kabulü için kalan işler

1. Farm B girdileri yalnız Scorer'ın private holdout yolunda kalacak.
   İnsan inceleme raporları araştırma ajanı bağlamına/dev manifestine
   taşınmayacak; ajan yalnız izinli sonuç bitini alacak.
2. Ölçülmüş baseline kalibrasyonu ve gerçek Farm B skorlama, 10 KEEP,
   run-end, rollback, atomik kota, restart/stop ve canary sızıntısı
   kontrolleri gereklidir. Kayıt/readback provası bunları kapatmaz.
3. Kaynaklar ana 0.30 sürümüne alındı. Gerçek süreç/Docker kurtarması ve
   paylaşılan host üzerindeki tam çalışma kaynak ölçümleri ayrıca tamamlanmalıdır.
