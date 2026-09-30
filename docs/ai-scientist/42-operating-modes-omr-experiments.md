# Çalışma modları, OMR ve deney laboratuvarı

Tarih: 2026-09-27. Kullanıcının güncel kapsamı; önceki M0 kabulünün yerine geçmez.
Durum: **kısmi uygulama**. İstatistik modülü, üç yöntemin ilk CPU ölçümleri
ve tek snapshot için bağlı arayüz/Director/Scorer projesi tamamlandı;
geniş bağlı deney havuzu ve uzun deney kabulü sürüyor.
Planlanan testlerle gerçekten çalıştırılmış sonuçlar aşağıda ayrı kaydedilir.

## Kullanılacak akış

Veri kaynağı/sentetik üretici → veri seçimi ve kalite/istatistik önizlemesi →
sürümlü snapshot ve zaman bölmeleri → LSH / OPTICS / SOM ile normal çalışma
modları → mod toleransları → yeni nokta için nearest neighbor tahmini →
sensör residual'ları ve sürekli OMR → alarm, karşılaştırma ve deney raporu.

Üç yöntem aynı girdilerle değiştirilebilir olmalıdır. LSH yalnız hızlı komşu
araması olarak eklenip çalışma modu özelliği tamamlandı sayılmaz; hash kovaları
ile birleştirilmiş referans kümelerinin nasıl mod oluşturduğu sürümlenir.
OPTICS eğitim kümesindeki yoğunluk yapısını kullanır; yeni noktada yeniden fit
yapılmaz. SOM birimlerinin/modlarının eşlemesi, doluluğu ve komşuluğu görünürdür.
Noise/bilinmeyen mod açık bir sonuçtur; her nokta zorla bilinen moda sokulmaz.

## Doğrulanmış kaynaklar

- AVEVA'nın 2022 teknik sunumu, geçmiş verinin kümelenmesini, yeni noktanın
  modellenmiş koşullarla karşılaştırılmasını ve sensör katkılarını gösterir.
  Sayfa 12'de bağıl sapma eğitim aralığına bölünen mutlak farktır; OMR bu
  bağıl sapmaların RMS değeridir ve yüzde olarak sunulur.
  [AVEVA teknik sunumu, sayfa 9–13](https://cdn.osisoft.com/osi/presentations/2022-AVEVA-Amsterdam/UC22EU-D3WI010-AVEVA-Petrone-Get-Integrated-Connect-AI-to-Your-AVEVA-PI-System.pdf#page=9).
- AVEVA'nın Çolakoğlu sunum özeti, LSH ile öğrenilen sağlıklı davranıştan
  sapma tespitini ve OMR kullanımını açıkça belirtir.
  [AVEVA Çolakoğlu örneği](https://www.aveva.com/en/perspectives/presentations/2025/colakoglu-metalurji-a-s---predictive-maintenance-for-industrial-cranes---implementing-event-driven-analytics-to-reduce-downtime/).
- AVEVA'nın 2021 sunumunda OPTICS, ilişkili model girdilerinin alt gruplara
  ayrılması için anlatılır. Bu, bizim zaman noktalarını çalışma modlarına
  ayırma kullanımımızın ürünle birebir eşdeğerliğini kanıtlamaz.
  [AVEVA 2021 sunumu](https://cdn.osisoft.com/osi/presentations/2021-aveva-pi-world/UC21NA-D0PI020-AVEVA-Gregerson-AVEVA-Predictive-Analytics.pdf).
- TrendMiner 2024.R3 belgesi deneysel SOM modelini normal çalışma verisiyle
  eğitir; quantization/topological hata takibi, en yakın SOM birimine uzaklık
  skoru ve yüzdelik eşikle anomali sınıfı üretir. Bu uzaklık skoru OMR değildir.
  Doküman bir ürün özelliğinin referansıdır; bu projede özel TrendMiner paketi
  veya ürünle sayısal eşdeğerlik varsayılmaz.
  [TrendMiner SOM modeli](https://userguide.trendminer.com/2024.R3.0/en/experimental--trendminer-anomaly-detection-model.html).

Kamuya açık kaynaklar mod toleranslarının bütün ayrıntılarını ve nearest
neighbor ağırlıklarını açıklamıyor. Aşağıdaki seçimler bizim açık, sürümlü
uygulama sözleşmemizdir; AVEVA'nın özel uygulaması olarak sunulmaz.

## Tahmin ve skor sözleşmesi

- Normal referanslar, ölçekler ve toleranslar yalnız eğitim verisinden öğrenilir.
  Değerlendirme verisi ya da gelecek noktalar model fit'ine katılmaz.
- Mod seçimi uzaklığı, mod kabul toleransı ve OMR alarm eşiği ayrı ayarlardır.
  Mod toleransı eğitim referanslarının uzaklık dağılımından belirlenir;
  yüzdelik ve çarpan ayarlanabilir. Mod başına örnek sayısı ve tolerans raporlanır.
- Uygun mod içindeki referanslardan k-nearest-neighbor ile tahmin üretilir.
  `k=1` en yakın normal referanstır; `k>1` için uniform/distance ağırlığı açık
  parametredir. Eşit uzaklıklar deterministik çözülür. Sıfır uzaklık güvenli
  hesaplanır. Tahmin mevcut noktanın beklenen normal değeridir; gelecekteki
  bir zamana ait tahmin olarak etiketlenmez.
- Hiçbir mod toleransı karşılanmıyorsa `out_of_mode` gösterilir. OMR için en
  yakın normal moda göre referans skor üretilebilirse bu koşul yanında kalır;
  hesaplanamayan durumda `null` ve neden kullanılır, sıfır/sağlıklı yazılmaz.
- Her sensör için gerçek değer, tahmin, işaretli fark ve mutlak fark döner.
  Eğitim aralığı `R_j = max(train_j) - min(train_j)` olarak dondurulur.

```text
relative_deviation_j = abs(x_j - prediction_j) / R_j
OMR_percent = 100 * sqrt(mean(relative_deviation_j ** 2))
contribution_j = relative_deviation_j ** 2 / sum(relative_deviation ** 2)
```

- Katkı payları bizim açıklama çıktımızdır; sıfır toplam residual'da hepsi sıfırdır.
  Yüzde OMR 100'ü aşabilir, kırpılmaz. MAD/z-score ile normalleştirilmiş başka
  skorlar aynı OMR adıyla sunulmaz.
- Sabit sensör için sıfıra bölme yapılmaz. Sabit kalan sensör ayrı kalite
  bilgisiyle görünür; değişmesi ayrıca sapma olarak işaretlenir. Geçerli OMR
  sensör kümesi, dışlanan sensör ve gerekçesi raporlanır. Tüm sensörler
  sabitse OMR hesaplanamadı sonucu üretilir; sessizce başarılı sayılmaz.
- Eğitim noktası kendi komşusu seçilerek bütün kalibrasyon residual'ları
  sıfırlanmaz: eşik kalibrasyonu için kronolojik ayrılmış sağlıklı pencere
  veya açık leave-one-out stratejisi sürümlenir.
- Sürekli hesaplama, dondurulmuş model üzerinde ardışık veri işleme demektir.
  Batch/chunk/tek-nokta sonuçları eşit olmalıdır; model güncellemesi ayrı deneydir.

## Ayarlanabilir deneyler

| Bileşen | Deney parametreleri |
|---|---|
| LSH | tablo/projeksiyon sayısı, kova genişliği, birleşim ve minimum mod desteği |
| OPTICS | min_samples, max_eps, xi, min_cluster_size, açık uzaklık metriği |
| SOM | grid boyutları, iterasyon, öğrenme oranı, komşuluk sigma, seed |
| Mod toleransı | eğitim yüzdeliği, tolerans çarpanı, minimum destek |
| Nearest neighbor | k, uniform/distance ağırlığı, mesafe metriği |
| Alarm | kalibrasyon yüzdeliği, dwell, release/histerezis |

Mevcut robust-z, Isolation Forest ve ECOD karşılaştırmaları korunur. Yeni
yöntemler kalibrasyon referanslarını gizlice değiştirmez. Ajan parametre
adaylarını seçer; ölçüm, bütçe ve karar mevcut Scorer/Director yolundan geçer.
Kaynak sınırı olan grid/random araması, deney kaydı, iptal, devam ve rapor
aynı koşu kimliği altında izlenmelidir.

## Veri ve temel istatistikler

Kaynak türü için kullanıcı yanıtı bekleniyor; bağımsız geliştirme yerel örnek
veritabanı ve sentetik snapshot ile sürer. İlk bağlantı varsayımı PostgreSQL'dir.
Gerçek kaynak yapılandırılmadan gerçek veri bağlantısı kabulü verilmez.

- Kaynak/tablo/tag, varlık, UTC zaman aralığı ve kolon seçimi; örnekleme,
  birim, veri kalitesi, satır ve bellek sınırı görünür olmalıdır.
- Okuma hesabı ve sınırlı seçim reçetesi kullanılır. Snapshot kaynak sorgusunu,
  zaman aralığını, veri sürümünü ve hash'i kaydeder; parola rapora girmez.
- Sensör bazında toplam/geçerli/eksik sayısı, eksik oranı, tekrarlı zaman,
  boşluk, örnekleme aralığı, minimum/maksimum, ortalama/medyan, std/varyans,
  MAD/IQR, yüzdelikler, çarpıklık ve basıklık sunulur. Küçük örneklem veya
  sabit seri için tanımsız istatistikler nedenleriyle `null` döner.
- Histogram, dağılım/zaman grafiği, Pearson/Spearman korelasyonları ve her
  çiftin geçerli örnek sayısı; otokorelasyon, trend ve dönem karşılaştırması.
  Zaman bazlı analiz düzensiz örnekleme/boşluk etkisini açık gösterir.
- İstatistikler sensör, varlık, dönem ve öğrenilen çalışma modu bazında
  incelenebilir. Korelasyon nedensellik iddiası değildir. Eğitimden öğrenilen
  dönüşümler değerlendirme istatistikleriyle güncellenmez.

## Geniş kanıt/test havuzu

Başlangıç matrisi: **10 senaryo × 3 mod yöntemi × 3 seed = 90 koşul**.
Hiperparametre kombinasyonları buna eklenebilir; kaynak bütçesi aşılırsa iş
kuyrukta kalır. Aynı seed bağımsız saha kanıtı olarak sayılmaz.

1. Sağlıklı tek mod: false alarm ve residual dağılımı.
2. Sağlıklı çok mod: mod geçişi, mod desteği, yanlış alarm.
3. Sağlıklı yük/ortam değişimi: koşula bağlı normal davranış.
4. Basamak sapması: olay yakalama ve sensör katkısı.
5. Yavaş drift: algılama gecikmesi ve OMR eğrisi.
6. Varyans artışı: duyarlılık/yanlış alarm dengesi.
7. Korelasyon kırılması: tek değişken sınırları içinde çok değişkenli sapma.
8. Salınım ve gecikme: zaman davranışı ve nedensellik.
9. Görülmemiş çalışma modu: tolerans dışı durumun açık gösterilmesi.
10. Eksik/sabit/arızalı sensör: kalite bayrakları, geçersiz skor ve maskeler.

Üretici parametreleri ve beklenen olay/mod etiketleri saklanır; etiketler
Scorer tarafında kalır. Sensör arızaları süreç anomalisi başarısını yapay
artıracak biçimde havuza katılmaz. Hedef; bilinen formül örnekleri, seed
tekrarlanabilirliği, batch/stream eşitliği, fit/eval ayrımı, tolerans sınırları,
çoklu seed sonuçları ve durdur/devam/süre bütçesi davranışını ölçmektir.

## Tamamlanma kanıtı

| Kabul | Durum | Gerekli kanıt |
|---|---|---|
| OM.1 sentetik üretim ve DB seçimi | Kısmi | 30 sentetik snapshot; gerçek read-only PostgreSQL ve arayüzde sensör/dönem seçimi geçti |
| OM.2 LSH/OPTICS/SOM çalışma modları | Kısmi | 90 sayısal koşu; tek snapshot için üç özelleştirilmiş yöntem arayüz/Director/Scorer üzerinden tamamlandı |
| OM.3 NN ve sürekli OMR | Kısmi | tamamlanmış raporda OMR, NN tahmini, residual ve sensör katkıları geçti; uzun süreli akış açık |
| OM.4 istatistik ve SOM tanıları | Kısmi | 12 istatistik testi; 18 özellik, histogram, korelasyon, 12 ACF ve üç yöntemin ölçülmüş tanıları arayüzde geçti |
| OM.5 geniş deney havuzu | Kısmi | 90 sayısal sonuç ve tek snapshot için 12 gerçek Scorer ölçümü; geniş havuzun tamamını Director/Scorer üzerinden tekrar açık |
| OM.6 uzun agent deneyi | Açık | kayıtlı bütçe, yerel LLM önerileri, durdur/devam ve rapor |
| OM.7 kullanıcı arayüzü | Kısmi | veri seçimi/istatistik/parametreli deney/tamamlanmış mod raporu ve JSON indirme geçti; uzun agent akışı açık |

Sentetik akışın geçmesi gerçek santral genellemesini, saha DB bağlantısını
veya AOS/GPU birlikte kullanımını tamamlanmış yapmaz. Sentetik veriye gerçek
PostgreSQL erişimi ayrıca ölçüldü. Önceki M0 maddeleri
`m0-acceptance.md` içinde ayrıca açık kalır.

### İlk istatistik uygulaması

`lab/analytics/statistics.py` içinde ana sürüme alınan uygulama hazır:
`summarize_dataset` seçilen sensörler ve zaman kolonu için JSON-safe rapor
üretir. Bilinen sayısal sonuçlar, değişmeden kalan girdi, eksik/sonsuz/sabit
sensörler, düzensiz/naive/tekrarlı zaman, eşleşmiş korelasyon örnekleri ve
geçersiz kolon türleri için **12 test gerçekten geçti**. Strict mypy, Pylint,
Bandit ve düzeltilmiş son Ruff kontrolü exit 0. Son toplu kontrolün outer
exit 1'i yalnız test import boşluğu nedeniyleydi; düzeltme ve son Ruff sonucu
ayrıca kaydedildi. Ardından birleşik 0.34 kalite kapısı 708 test ve yedi
komutla exit 0 verdi; kaynaklar byte olarak aynı şekilde ana koda alındı.
0.35.1 arayüzünde PostgreSQL sensör alt kümesi, 18 ayrıntılı özellik,
histogramlar, Pearson/Spearman ve 12 gecikmeli ACF gösterimi, dönem seçimi
ve JSON indirme gerçek tarayıcıda geçti. Veriler sentetiktir.
[Tarayıcı kanıtı](review-evidence/operating-console045-browser-r3.json).
[Kaynak ve kontrol kaydı](review-evidence/statistics040-source-checks.json).

Gerçek PostgreSQL üzerinde, ayrı yalnız-okuma hesabı ve izinli görünümden
**288 sentetik satır / 4 sensör** seçildi. Üretim okuyucusu zaman ve makine
filtresiyle 192 eğitim + 96 değerlendirme satırı döndürdü; iki dönem için
istatistik, korelasyon ve düzenli UTC zaman analizi ayrı JSON raporlarına
yazıldı. **Session 46560 / exit 0**, kaynaklar sabit. İlk sürücü denemesi
Python list/tuple dönüşümünde SQL okumadan önce başarısızdı; yalnız sürücü
JSON parse kullanacak şekilde düzeltildi. Bu gerçek DB taşımasıdır; veriler
yerel sentetiktir, saha verisi veya otonom ajan/tarayıcı kabulü değildir.
[DB seçimi ve istatistik kanıtı](review-evidence/statistics040-postgres-source-proof.json).

Gerçek seçili kamu kaynaklarının eğitim bölümleri de aynı üretim analiz
modülünden geçirildi: Genesis 4.055 × 18, GECCO 16.165 × 9,
CATSv2 16.568 × 17, SMD 12.000 × 38. Toplam 82 sensör ve 1.028 sensör
çifti analiz edildi; session **49099 / exit 0**, analiz 1,478 saniye,
process tepe belleği 386.420.736 bayt. Girdiler değişmedi ve model çağrısı
yapılmadı. Manifest özgün zaman damgası taşımadığı için zaman trendi ve ACF
gerekçeli `null` kaldı; takvim/zaman değerleri üretilmedi. Dört JSON'da
kaynak/split kimliği ve atıf bilgisi korunur.
[Kanıt](review-evidence/public050-training-statistics-proof.json),
[JSON indirme rehberi](43-first-project-guide.md#gerçek-kamu-verisinde-istatistik-örnekleri).

### İlk 90 gerçek sayısal koşu

Üç yöntem, on sentetik senaryo ve üç seed ile **90 koşulun tamamı çalıştı**.
Her koşul 192 eğitim + 96 değerlendirme satırı içeriyor. Kaynak hash'leri
sabit; process exit 0, toplam servis süresi 9,341 saniye, tepe bellek 155,4 MB,
swap 0. Bunlar doğrudan model fonksiyonlarıyla yapılan CPU ölçümleridir;
Director/Scorer/arayüz akışı ve uzun agent koşusu kabulü değildir.

| Varsayılan yöntem | Sağlıklı senaryolarda yanlış alarm oranı | Anomali noktalarında recall |
|---|---:|---:|
| LSH | %2,89 | %92,01 |
| OPTICS | %2,78 | %61,63 |
| SOM | %3,01 | %93,92 |

Sonuçlar yalnız bu kısa sentetik havuza ve kayıtlı varsayılan parametrelere
aittir. Sensör kalitesi arızaları süreç anomalisi başarı sayımına katılmaz.
Recall nokta bazındadır; olay yakalama oranı veya saha genellemesi değildir.
Her koşulun snapshot, model özeti, sensör residual'ları, OMR dizisi ve
parametreleri `data/runtime/parallel-m0/operating-modes-proof-042/matrix-8d61ce27d3be`
altında bulunur; `index.html` tabloyu açar.
[90 koşulun sonuçları](review-evidence/operating-modes042-matrix.json) ve
[gerçek yürütme kaydı](review-evidence/operating-modes042-execution-binding.json).

### Tamamlanan bağlı mod projesi

0.35.3 ile `716a8941-e913-4307-982d-f643e800a216` koşusu **completed** oldu.
Üç baseline × üç seed ve LSH/OPTICS/SOM için birer aday ile toplam **12
gerçek Docker/Scorer ölçümü**, 6 geçerli deney/trajectory çifti ve 0 model
token kaydedildi. Sentetik `step`, seed 0, 192 eğitim + 96 değerlendirme
satırı kullanıldı; eğitimden türetilen 10 satırlık embargo sonrası 86 satır
skorlandı. LSH tablo sayısı 3, OPTICS min_samples 6, SOM iterasyonu 200,
NN komşu sayısı 3; diğer ayarlar varsayılan.

Başlatma/istatistik için 12 ve tamamlanmış rapor için 7 gerçek tarayıcı
kontrolü geçti. Üç yöntemin OMR ve sensör tanıları, nokta seçimi, tam rapor
indirme ve masaüstü/mobil taşma kontrol edildi. API/DB/kanonik rapor hash'i
eşleşti: `37c5ce02e614f3c2d7a8a645e6b56367c06d24d4c7dc09771fd5ac37849b0cd4`.
Bu `single_snapshot_study`; `benchmark_acceptance=false`. İlk kurulum R3'te
yapılmıştı, son tarayıcı koşusu kurulu snapshot'ı kullandı. Önceki hatalar
aşağıda korunur.

[Proje kanıtı](review-evidence/operating-console045-completed-project.json),
[tam rapor](review-evidence/console0353-completed-mode-report.json),
[deneme rehberi](43-first-project-guide.md).

### Bağlı mod deneyi: ilk hata kaydı

0.35.1 gerçek tarayıcı akışında 12 kontrol geçti: sentetik kayıt, üç yöntem,
özelleştirilmiş LSH/OPTICS/SOM parametreleri ve mobil/masaüstü taşma kontrolü
dahil. Ancak `404d9a45-f1fb-4f91-a842-0edcd44bc1b9` koşusunun ilk baseline
ölçümü, Scorer UTC saat dilimli zamanı reddettiği için `stop_requested`
durumuna geçti. Sıfır bağımsız skor vardır; bu koşu tamamlanmış mod deneyi
sayılmaz. Kayıt, önceki başarısız kurulum ve tarayıcı sürücüsü denemeleri
korunuyor; düzeltme sonrası yeni ölçüm ayrıca kaydedilecek.

0.35.2 düzeltmesinden sonra `d61b2edb-80a3-4064-b9e4-64eec4834f40`
koşusunda üç baseline × üç seed ile **9 bağımsız ölçüm** tamamlandı. UTC
sorunu bu gerçek Scorer yolunda kapandı. Ardından kayıtlı holdout kontrolü,
eski SQL işlevindeki var olmayan alan nedeniyle başarısız oldu; koşu
`failed`, aday sayısı sıfır ve terminal rapor yok. Bu sonuç tamamlanmış
LSH/OPTICS/SOM deneyi değildir.
[0.35.2 tarayıcı ve koşu kaydı](review-evidence/operating-console045-browser-r4.json).

İlk başarısız koşunun salt okunur recovery incelemesi, normal sürüm
geçişinde eski sahip süreç kapandıktan sonra yapıldı. Sahip ölümünün kanıtı
var; kuyruk işi ve eksik terminal belgeler kapanışı engelliyor. `apply`
çalıştırılmadı; desteklenen kapanış yolu 046 kapsamındadır.
[Recovery incelemesi](review-evidence/operating-console045-failed-grid-inspection.json).
