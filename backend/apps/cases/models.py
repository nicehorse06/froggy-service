import uuid
from django.utils.translation import ugettext_lazy as _
from django.utils import timezone, formats
from django.db.models.signals import post_save
from django.contrib.sites.models import Site
from django.urls import reverse
from django.db.models import (
    Model,
    CASCADE,
    CharField,
    DateTimeField,
    TextField,
    ForeignKey,
    EmailField,
    QuerySet,
    SET_NULL,
    UUIDField,
    IntegerField,
)

from django_fsm import FSMField, transition
from tagulous.models import TagField

from apps.cases.slack import new_case_notify
from apps.mails.models import SendGridMail, SendGridMailTemplate
from apps.files.models import TempFile


class Type(Model):
    """案件類別"""
    name = CharField(max_length=50, verbose_name=_('Name'))
    update_time = DateTimeField(auto_now=True, null=True, blank=True, verbose_name=_('Updated Time'))

    class Meta:
        verbose_name = _('Case Type')
        verbose_name_plural = _('Case Type')
        ordering = ('id',)

    def __str__(self):
        return self.name


class Region(Model):
    """使用者所在選區"""
    name = CharField(max_length=50, verbose_name=_('Name'))
    update_time = DateTimeField(auto_now=True, null=True, blank=True, verbose_name=_('Updated Time'))

    class Meta:
        verbose_name = _('User Region')
        verbose_name_plural = _('User Region')
        ordering = ('id',)

    def __str__(self):
        return self.name


class State(object):
    """案件狀態"""
    DRAFT = 'draft'
    DISAPPROVED = 'disapproved'
    ARRANGED = 'arranged'
    CLOSED = 'closed'

    CHOICES = (
        (DRAFT, '尚未成案'),
        (DISAPPROVED, '不受理'),
        (ARRANGED, '處理中'),
        (CLOSED, '已結案'),
    )


class Priority(object):
    """案件優先程度"""
    LOWEST = 1
    LOW = 2
    NORMAL = 3
    HIGH = 4
    HIGHEST = 5

    CHOICES = (
        (LOWEST, '最低'),
        (LOW, '低'),
        (NORMAL, '普通'),
        (HIGH, '高'),
        (HIGHEST, '最高'),
    )


class CaseQuerySet(QuerySet):
    """
    為了觸發post_save和pre_save，複寫Queryset的update函式，使用情境例如：
        Case.objects.filter(**kwargs).update(**kwargs)
    此改寫須迭代從資料庫取出、寫入物件，會導致時間複雜度比原生方法差上許多
        O(1) --> O(更新個數*更新欄位數)
    """
    def update(self, **kwargs):
        for case in self.all():
            for key, value in kwargs.items():
                setattr(case, key, value)
            case.save()


class Case(Model):
    """案件
    * state: 案件狀態, 預設值為未成案
    * uuid: 案件編號（uuid4）
    * type: 案件類別
    * region: 使用者所在選區
    * title: 標題
    * content: 案件內容
    * location: 相關地址
    * username: 使用者名字
    * mobile: 手機
    * email: 信箱
    * address: 地址
    * open_time: 成案日期
    * close_time: 結案日期
    * update_time: 上次更新時間
    """
    state = FSMField(default=State.DRAFT, verbose_name=_('Case State'), choices=State.CHOICES)
    priority = IntegerField(default=Priority.NORMAL, verbose_name=_('Case Priority'), choices=Priority.CHOICES)
    uuid = UUIDField(default=uuid.uuid4, verbose_name=_('UUID'), unique=True)
    number = CharField(max_length=6, default='-', null=True, blank=True, verbose_name=_('Case Number'))
    type = ForeignKey('cases.Type', on_delete=CASCADE, related_name='cases', verbose_name=_('Case Type'))
    region = ForeignKey('cases.Region', on_delete=CASCADE, related_name='cases', verbose_name=_('User Region'))
    title = CharField(max_length=255, verbose_name=_('Case Title'))
    content = TextField(verbose_name=_('Content'))
    location = CharField(null=True, blank=True, max_length=255, verbose_name=_('Location'))
    username = CharField(max_length=50, verbose_name=_('Username'))
    mobile = CharField(max_length=10, null=True, blank=True, verbose_name=_('Mobile'))
    email = EmailField(null=True, blank=True, verbose_name=_('Email'))
    address = CharField(null=True, blank=True, max_length=255, verbose_name=_('Address'))
    open_time = DateTimeField(null=True, blank=True, verbose_name=_('Opened Time'))
    close_time = DateTimeField(null=True, blank=True, verbose_name=_('Closed Time'))
    create_time = DateTimeField(auto_now_add=True, null=True, blank=True, verbose_name=_('Created Time'))
    update_time = DateTimeField(auto_now=True, null=True, blank=True, verbose_name=_('Updated Time'))

    disapprove_info = TextField(null=True, blank=True, verbose_name=_('Disapprove Info'))

    note = TextField(null=True, blank=True, verbose_name=_('Case Notes'))
    tags = TagField(blank=True, verbose_name=_('Tags'), verbose_name_plural=_('Tags'))

    objects = CaseQuerySet.as_manager()

    class Meta:
        verbose_name = _('Case')
        verbose_name_plural = _('Cases')
        ordering = ('id',)

    def save(self, *args, **kwargs):
        created = self.pk is None
        super(Case, self).save(*args, **kwargs)

        if created:
            self.number = str(self.pk).zfill(6)
            self.save()
            self.migrate_files_from_temp_storage()  # 搬移暫存檔案
            self.confirm(template_name='收件通知')  # 發送確認信
            new_case_notify(self)  # 發送slack通知

    def __str__(self):
        return self.number

    def to_dict(self):
        """用於新增CaseHistory"""
        return {
            'state': self.state,
            'title': self.title,
            'type': self.type,
            'region': self.region,
            'content': self.content,
            'location': self.location,
            'username': self.username,
            'mobile': self.mobile,
            'email': self.email,
            'address': self.address,
            'priority': self.priority,
        }

    def migrate_files_from_temp_storage(self):
        temp_files = TempFile.objects.filter(case_uuid=self.uuid)
        for temp_file in temp_files:
            temp_file.migrate_to_case(self)

    @property
    def first_history(self):
        """回傳最早的案件歷史，用於存取原始資料"""
        return self.case_histories.order_by('create_time').first()

    @property
    def state_title(self):
        for state, title in State.CHOICES:
            if self.state == state:
                return title
        return ''

    @property
    def admin_absolute_url(self, action='change'):
        admin_url = reverse(f'admin:cases_case_{action}', args=(self.id,))
        return f'{Site.objects.get_current().domain}{admin_url}'

    def format_create_time(self, format_='SHORT_DATETIME_FORMAT'):
        return formats.date_format(self.create_time, format_)

    ########################################################
    # Transition Conditions
    # These must be defined prior to the actual transitions
    # to be reference.

    def can_disapprove(self):
        return bool(self.disapprove_info)
    can_disapprove.hint = '填寫不受理資訊後方能設為不受理'

    def can_arrange(self):
        return self.case_histories.all().count() > 1
    can_arrange.hint = '案件編輯過至少一次才能成案'

    def can_close(self):
        arranges = self.arranges.all()
        return arranges and all([arrange.published for arrange in arranges])
    can_close.hint = '全部的處理進度都發布後才能結案'

    ########################################################
    # Workflow (state) Transitions

    def confirm(self, template_name):
        """寄送確認信"""
        first = self.first_history or self
        data = {
            'number': self.number,
            'username': first.username,
            'title': first.title,
            'datetime': self.format_create_time(),
            'content': first.content,
            'location': first.location,
        }
        template = SendGridMailTemplate.objects.get(name=template_name)
        SendGridMail.objects.create(case=self, template=template, data=data)

    @transition(field=state, source=State.DRAFT, target=State.DISAPPROVED, conditions=[can_disapprove],
                permission=lambda instance, user: user.has_perm('cases.change_case'),
                custom={'button_name': '設為不受理'})
    def disapprove(self):
        first = self.first_history or self
        data = {
            'number': self.number,
            'username': first.username,
            'title': first.title,
            'datetime': self.format_create_time(),
            'content': self.disapprove_info,
        }
        template = SendGridMailTemplate.objects.get(name='不受理通知')
        SendGridMail.objects.create(case=self, template=template, data=data)
        self.close_time = timezone.now()

    @transition(field=state, source=State.DRAFT, target=State.ARRANGED, conditions=[can_arrange],
                permission=lambda instance, user: user.has_perm('cases.change_case'),
                custom={'button_name': '設為處理中'})
    def arrange(self):
        self.confirm(template_name='成案通知')
        self.open_time = timezone.now()

    @transition(field=state, source=State.ARRANGED, target=State.CLOSED, conditions=[can_close],
                permission=lambda instance, user: user.has_perm('cases.change_case'),
                custom={'button_name': '設為已結案'})
    def close(self):
        first = self.first_history or self
        data = {
            'number': self.number,
            'username': first.username,
            'case_title': first.title,
            'arranges': [
                {
                    'title': arrange.title,
                    'datetime': arrange.format_arrange_time(),
                    'content': arrange.email_content,
                }
                for arrange in self.arranges.all()
            ],
        }
        template = SendGridMailTemplate.objects.get(name='結案通知')
        SendGridMail.objects.create(case=self, template=template, data=data)
        self.close_time = timezone.now()

    @transition(field=state, source=[State.DISAPPROVED, State.CLOSED], target=State.ARRANGED, conditions=[can_arrange],
                permission=lambda instance, user: user.has_perm('cases.change_case'),
                custom={'button_name': '設回處理中'})
    def rearrange(self):
        self.confirm(template_name='成案通知')
        now = formats.date_format(timezone.now(), 'SHORT_DATETIME_FORMAT')
        if self.state == 'disapproved':
            self.disapprove_info += f'(已於{now}設回處理中)'
        self.open_time = timezone.now()


def case_mode_save(sender, instance, *args, **kwargs):
    """案件新增與每次更新時建立案件歷史"""
    history, created = CaseHistory.objects.get_or_create(case=instance, **instance.to_dict())
    if created:
        # Get editor via admin save_model()
        if hasattr(instance, 'user'):
            history.editor = instance.user
            history.save()


post_save.connect(case_mode_save, sender=Case)


class CaseHistory(Model):
    """案件歷史，案件新增與每次更新時建立"""
    editor = ForeignKey('users.User', null=True, blank=True, on_delete=SET_NULL, related_name='case_histories',
                        verbose_name=_('Editor'))
    case = ForeignKey('cases.Case', on_delete=CASCADE, related_name='case_histories', verbose_name=_('Case'))
    state = FSMField(default=State.DRAFT, verbose_name=_('Case State'), choices=State.CHOICES, protected=True)
    priority = IntegerField(default=Priority.NORMAL, verbose_name=_('Case Priority'), choices=Priority.CHOICES)
    title = CharField(max_length=255, verbose_name=_('Case Title'))
    type = ForeignKey('cases.Type', on_delete=CASCADE, related_name='case_histories', verbose_name=_('Case Type'))
    region = ForeignKey('cases.Region', on_delete=CASCADE, related_name='case_histories', verbose_name=_('User Region'))
    content = TextField(verbose_name=_('Content'))
    location = CharField(null=True, blank=True, max_length=255, verbose_name=_('Location'))
    username = CharField(max_length=50, verbose_name=_('Username'))
    mobile = CharField(max_length=10, null=True, blank=True, verbose_name=_('Mobile'))
    email = EmailField(null=True, blank=True, verbose_name=_('Email'))
    address = CharField(null=True, blank=True, max_length=255, verbose_name=_('Address'))
    create_time = DateTimeField(auto_now_add=True, verbose_name=_('Created Time'))

    class Meta:
        verbose_name = _('Case History')
        verbose_name_plural = _('Case Histories')
        ordering = ('-create_time',)

    @property
    def number(self):
        return self.case.number

    def __str__(self):
        return self.case.number
