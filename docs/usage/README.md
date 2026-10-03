# Geliştirme ve uygulama tüketim kayıtları

## Hangi sayı neyi ölçüyor?

| Kayıt | Kaynak | Kullanım sınırı |
|---|---|---|
| Geliştirme tokenı | İlişkili Codex oturumlarının `token_count` sayaçları | API faturası veya tam proje ömrü değildir |
| Goal aktif süre/token | `get_goal`, dönemlerin son gözlemi | İnsan işçiliği değildir; API token toplamına eklenmez |
| Uygulama tüketimi | Açıkça seçilmiş gerçek çalışma receipt'leri | Diğer DB/koşuların toplamını kapsamaz |
| Gerçek ücret | Sağlayıcı fatura/kullanım dökümü | Belge yok; bilinmiyor |
| Abonelik | Abonelik/ödeme belgesi | Belge yok; bilinmiyor; tokenlardan hesaplanmaz |
| API fiyat senaryosu | Tarihli resmi tarife ve varsayımlar | Fatura değil; güncel Standard/kısa bağlam karşılığı |

Güncel [tablo](project-usage-latest.md) ve [makine okunur rapor](project-usage-latest.json)
sağlayıcı, tam model kimliği, effort ve UTC gün kırılımını içerir. Cached input,
input'un alt kümesidir; reasoning output, output'un alt kümesidir. İkinci kez
sayılmaz. Tekrarlanan büyük bağlamlar toplam tokena dahildir; toplam benzersiz
kelime sayısı değildir. Codex aboneliği üzerinden çalışmanın varsayımsal API
karşılığı gerçek ödeme olarak gösterilmez.

Collector `scripts/collect_project_usage.py`: metadata ve token olaylarını okur;
ham prompt/yanıtı rapora taşımaz. Aynı sayaç tekrarları, kopyalanmış parent
önekleri ve fork/reset belirsizliği ayrılır. Tamamlanmamış son satır sonraki
ölçüme bırakılır; kaynak dosyası değişimi/truncate veya tutarsız gözlem hata verir.
Eksik kayıtlar, model atfı bilinmeyen aralıklar ve dışlanan geçersiz sayaçlar
JSON'da açıkça kaydedilir. Bu yüzden eksik geçmişten yaşam boyu toplam üretilmez.

Ölçülen uygulama kapsamı şu üç kanıtla sınırlıdır: altı önerili yerel Qwen koşusu,
public DEV CPU deneyi ve saha amacı CPU deneyi. Yerel model tokenı API ücreti
oluşturduğu varsayımıyla fiyatlanmaz; elektrik/donanım maliyeti ölçülmedi.

## Düzenli kayıt ve yayın

`collect_project_usage.py` gözlemleri üretir. `hourly_project_usage.py` iki kullanım
raporunu kesin alan/tip/değer şemasıyla üretir ve aynı gözlemden README içindeki
`api-cost-summary` işaretli maliyet bölümünü günceller. Üç dosya tek commit içinde
yayımlanır. Publisher README bölümünü yeniden hesaplayarak bölüm dışındaki
baytların ve dosya modunun aynen korunduğunu doğrular. Eksik veya tekrar eden
işaretler reddedilir; önceki başarılı rapor korunur. `publish_reviewed_snapshot.py` incelenmiş, dosya hashleri sabit bir
paketi doğru public `origin/main` geçmişinde normal commit/push ile yayımlar.

Saatlik işlem canlı geliştirme ağacını `git add -A` ile toplamaz. Tamamlanan kod
değişiklikleri kalite/mahremiyet kontrolünden sonra ayrı incelenmiş pakete alınır;
otomatik kullanım güncellemesi son yayımlanmış kaynak kodu aynen korur. Anlamlı
kullanım veya incelenmiş kaynak değişikliği yoksa commit/push yapılmaz. Force push,
otomatik merge ve AOS deposunda işlem yoktur.

Remote değişmişse, checkout kirliyse, dosya hash'i/pin uyuşmuyorsa veya önceki
ölçüm geriliyorsa işlem durur. Hata/son işlem kaydı özel çalışma alanında tutulur;
son iyi public rapor korunur. Push yanıtı kaybolursa aynı commit doğrulanır,
ikinci kopya commit üretilmez. Geçmiş kapsam eksikliği raporda görünür kalabilir;
bu, yeni geçerli gözlemlerin kaydını durdurmaz.

Kod/rate/config değişikliğinde pinler yalnız yeni inceleme sonrası yenilenmelidir.
Timer kurulumunun gerçek durumu teslim receipt'inde belirtilir. Makine kapalıyken
saatlik işlem çalışmaz; kullanıcı systemd yöneticisinin ömrü oturum ayarına bağlıdır.
Ham oturumlar, özel config, DSN, SSH anahtarları, erişim tokenları ve çalışma
verileri public pakete alınmaz.

## Tarife kaynağı

[2026-10-03 fiyat varsayımları](api-prices-2026-10-03.json),
[OpenAI resmi API tarifesi](https://developers.openai.com/api/docs/pricing)
ve [Codex/API kullanım ayrımı](https://help.openai.com/en/articles/20001516-managing-usage-with-gpt-6-astra-in-work-and-codex).
Tarihsel tarife, gerçek servis katmanı, uzun bağlam sınıfı, vergiler/indirimler
ve ek araç ücretleri doğrulanmadı. Belge sağlanırsa gerçek ücret ayrıca eklenir;
varsayımsal senaryo geriye dönük fatura diye yeniden etiketlenmez.

## Hazır host üzerindeki saatlik görev

Kurulum birimleri `scientist-reviewed-publication.service` ve
`scientist-reviewed-publication.timer`; özel config/receipt alanı
`data/runtime/public-sync` dizinidir. Birimlerin gerçek etkinlik ve son çıkış
durumu teslim sırasında doğrulanır; kaynak dosyanın bulunması kurulum kanıtı değildir.

```bash
systemctl --user status scientist-reviewed-publication.timer
systemctl --user list-timers scientist-reviewed-publication.timer
journalctl --user -u scientist-reviewed-publication.service -n 20 --no-pager
```

Görev yarım CPU ve 1 GiB RAM ile sınırlandırılır. Başlamadan en az 22 GiB boş
disk ister; hostun 20 GiB rezervi ve geçici işlem payı korunur. Yetersiz alan
hata verir, başka iş/cache/veri silinmez. İncelenmiş tam paketler ACK sonrasında
da özel arşivde tutulur: anlamlı her yayın yaklaşık 60 MiB ek yer tutabilir.
Değişmeyen rapor arşiv oluşturmaz. Uzun süreli aktif geliştirmede arşiv kapasitesi
izlenmeli; ACK edilmiş paketlerin saklama politikası sonraki işletim işidir.

Zamanlayıcıyı durdurmak yalnız bu yayını kapatır; Lab/AOS çalışmasını etkilemez:

```bash
systemctl --user disable --now scientist-reviewed-publication.timer
```
