import logging
import re
from typing import Union, List, Dict, Any, Tuple

from atlasclient.client import Atlas
from atlasclient.exceptions import BadRequest
from atlasclient.models import EntityUniqueAttribute
from atlasclient.utils import parse_table_qualified_name, make_table_qualified_name
from flask import current_app as app
from beaker.cache import CacheManager
from beaker.util import parse_cache_config_options
from random import randint

from metadata_service.entity.popular_table import PopularTable
from metadata_service.entity.table_detail import Table, User, Tag, Column, Statistics
from metadata_service.entity.tag_detail import TagDetail
from metadata_service.entity.user_detail import User as UserEntity
from metadata_service.exception import NotFoundException
from metadata_service.proxy import BaseProxy
from metadata_service.util import UserResourceRel

LOGGER = logging.getLogger(__name__)

# Expire cache every 11 hours + jitter
_ATLAS_PROXY_CACHE_EXPIRY_SEC = 11 * 60 * 60 + randint(0, 3600)

_CACHE = CacheManager(**parse_cache_config_options({'cache.regions': 'atlas_proxy',
                                                    'cache.atlas_proxy.type': 'memory',
                                                    'cache.atlas_proxy.expire': _ATLAS_PROXY_CACHE_EXPIRY_SEC}))


# noinspection PyMethodMayBeStatic
class AtlasProxy(BaseProxy):
    """
    Atlas Proxy client for the amundsen metadata
    {ATLAS_API_DOCS} = https://atlas.apache.org/api/v2/
    """
    TABLE_ENTITY = app.config['ATLAS_TABLE_ENTITY']
    DB_ATTRIBUTE = app.config['ATLAS_DB_ATTRIBUTE']
    QN_KEY = 'qualifiedName'
    ATTRS_KEY = 'attributes'
    REL_ATTRS_KEY = 'relationshipAttributes'

    def __init__(self, *,
                 host: str,
                 port: int,
                 user: str = 'admin',
                 password: str = '') -> None:
        """
        Initiate the Apache Atlas client with the provided credentials
        """
        self._driver = Atlas(host=host, port=port, username=user, password=password)

    def _get_ids_from_basic_search(self, *, params: Dict) -> List[str]:
        """
        FixMe (Verdan): UNUSED. Please remove after implementing atlas proxy
        Search for the entities based on the params provided as argument.
        :param params: the dictionary of parameters to be used for the basic search
        :return: The flat list of GUIDs of entities founds based on the params.
        """
        ids = list()
        search_results = self._driver.search_basic(**params)
        for result in search_results:
            for entity in result.entities:
                ids.append(entity.guid)
        return ids

    def _get_flat_values_from_dsl(self, dsl_param: dict) -> List:
        """
        Makes a DSL query asking for specific attribute, extracts that attribute
        from result (which is a list of list, and converts that into a flat list.
        :param dsl_param: A DSL parameter, with SELECT clause
        :return: A Flat list of specified attributes in SELECT clause
        """
        attributes: List = list()
        _search_collection = self._driver.search_dsl(**dsl_param)
        for collection in _search_collection:
            attributes = collection.flatten_attrs()
        return attributes

    def _extract_info_from_uri(self, *, table_uri: str) -> Dict:
        """
        Extracts the table information from table_uri coming from frontend.
        :param table_uri:
        :return: Dictionary object, containing following information:
        entity: Type of entity example: rdbms_table, hive_table etc.
        cluster: Cluster information
        db: Database Name
        name: Table Name
        """
        pattern = re.compile(r"""
            ^   (?P<entity>.*?)
            :\/\/
                (?P<cluster>.*)
            \.
                (?P<db>.*?)
            \/
                (?P<name>.*?)
            $
        """, re.X)
        result = pattern.match(table_uri)
        return result.groupdict() if result else dict()

    def _get_table_entity(self, *, table_uri: str) -> Tuple[EntityUniqueAttribute, Dict]:
        """
        Fetch information from table_uri and then find the appropriate entity
        The reason, we're not returning the entity_unique_attribute().entity
        directly is because the entity_unique_attribute() return entity Object
        that can be used for update purposes,
        while entity_unique_attribute().entity only returns the dictionary
        :param table_uri:
        :return: A tuple of Table entity and parsed information of table qualified name
        """
        table_info = self._extract_info_from_uri(table_uri=table_uri)
        table_qn = make_table_qualified_name(table_info.get('name'),
                                             table_info.get('cluster'),
                                             table_info.get('db')
                                             )

        try:
            return self._driver.entity_unique_attribute(
                table_info['entity'], qualifiedName=table_qn), table_info
        except Exception as ex:
            LOGGER.exception(f'Table not found. {str(ex)}')
            raise NotFoundException('Table URI( {table_uri} ) does not exist'
                                    .format(table_uri=table_uri))

    def _get_column(self, *, table_uri: str, column_name: str) -> Dict:
        """
        Fetch the column information from referredEntities of the table entity
        :param table_uri:
        :param column_name:
        :return: A dictionary containing the column details
        """
        try:
            table_entity, _ = self._get_table_entity(table_uri=table_uri)
            columns = table_entity.entity[self.REL_ATTRS_KEY].get('columns')
            for column in columns or list():
                col_details = table_entity.referredEntities[column['guid']]
                if column_name == col_details[self.ATTRS_KEY]['name']:
                    return col_details

            raise NotFoundException(f'Column not found: {column_name}')

        except KeyError as ex:
            LOGGER.exception(f'Column not found: {str(ex)}')
            raise NotFoundException(f'Column not found: {column_name}')

    def _serialize_columns(self, *, entity: EntityUniqueAttribute) -> \
            Union[List[Column], List]:
        """
        Helper function to fetch the columns from entity and serialize them
        using Column and Statistics model.
        :param entity: EntityUniqueAttribute object,
        along with relationshipAttributes
        :return: A list of Column objects, if there are any columns available,
        else an empty list.
        """
        columns = list()
        for column in entity.entity[self.REL_ATTRS_KEY].get('columns') or list():
            col_entity = entity.referredEntities[column['guid']]
            col_attrs = col_entity[self.ATTRS_KEY]
            statistics = list()
            for stats in col_attrs.get('stats') or list():
                stats_attrs = stats['attributes']
                statistics.append(
                    Statistics(
                        stat_type=stats_attrs.get('stat_name'),
                        stat_val=stats_attrs.get('stat_val'),
                        start_epoch=stats_attrs.get('start_epoch'),
                        end_epoch=stats_attrs.get('end_epoch'),
                    )
                )

            columns.append(
                Column(
                    name=col_attrs.get('name'),
                    description=col_attrs.get('description') or col_attrs.get('comment'),
                    col_type=col_attrs.get('type') or col_attrs.get('dataType'),
                    sort_order=col_attrs.get('position'),
                    stats=statistics,
                )
            )
        return columns

    def get_user_detail(self, *, user_id: str) -> Union[UserEntity, None]:
        pass

    def get_table(self, *, table_uri: str) -> Table:
        """
        Gathers all the information needed for the Table Detail Page.
        :param table_uri:
        :return: A Table object with all the information available
        or gathered from different entities.
        """
        entity, table_info = self._get_table_entity(table_uri=table_uri)
        table_details = entity.entity

        try:
            attrs = table_details[self.ATTRS_KEY]

            table_qn = parse_table_qualified_name(
                qualified_name=attrs.get(self.QN_KEY)
            )

            tags = []
            # Using or in case, if the key 'classifications' is there with a None
            for classification in table_details.get("classifications") or list():
                tags.append(
                    Tag(
                        tag_name=classification.get('typeName'),
                        tag_type="default"
                    )
                )

            columns = self._serialize_columns(entity=entity)

            table = Table(
                database=table_details.get('typeName'),
                cluster=table_qn.get('cluster_name', ''),
                schema=table_qn.get('db_name', ''),
                name=attrs.get('name') or table_qn.get("table_name", ''),
                tags=tags,
                description=attrs.get('description'),
                owners=[User(email=attrs.get('owner'))],
                columns=columns,
                last_updated_timestamp=table_details.get('updateTime'))

            return table
        except KeyError as ex:
            LOGGER.exception('Error while accessing table information. {}'
                             .format(str(ex)))
            raise BadRequest('Some of the required attributes '
                             'are missing in : ( {table_uri} )'
                             .format(table_uri=table_uri))

    def delete_owner(self, *, table_uri: str, owner: str) -> None:
        pass

    def add_owner(self, *, table_uri: str, owner: str) -> None:
        """
        It simply replaces the owner field in atlas with the new string.
        FixMe (Verdan): Implement multiple data owners and
        atlas changes in the documentation if needed to make owner field a list
        :param table_uri:
        :param owner: Email address of the owner
        :return: None, as it simply adds the owner.
        """
        entity, _ = self._get_table_entity(table_uri=table_uri)
        entity.entity[self.ATTRS_KEY]['owner'] = owner
        entity.update()

    def get_table_description(self, *,
                              table_uri: str) -> Union[str, None]:
        """
        :param table_uri:
        :return: The description of the table as a string
        """
        entity, _ = self._get_table_entity(table_uri=table_uri)
        return entity.entity[self.ATTRS_KEY].get('description')

    def put_table_description(self, *,
                              table_uri: str,
                              description: str) -> None:
        """
        Update the description of the given table.
        :param table_uri:
        :param description: Description string
        :return: None
        """
        entity, _ = self._get_table_entity(table_uri=table_uri)
        entity.entity[self.ATTRS_KEY]['description'] = description
        entity.update()

    def add_tag(self, *, table_uri: str, tag: str) -> None:
        """
        Assign the tag/classification to the give table
        API Ref: /resource_EntityREST.html#resource_EntityREST_addClassification_POST
        :param table_uri:
        :param tag: Tag/Classification Name
        :return: None
        """
        entity, _ = self._get_table_entity(table_uri=table_uri)
        entity_bulk_tag = {"classification": {"typeName": tag},
                           "entityGuids": [entity.entity['guid']]}
        self._driver.entity_bulk_classification.create(data=entity_bulk_tag)

    def delete_tag(self, *, table_uri: str, tag: str) -> None:
        """
        Delete the assigned classfication/tag from the given table
        API Ref: /resource_EntityREST.html#resource_EntityREST_deleteClassification_DELETE
        :param table_uri:
        :param tag:
        :return:
        """
        try:
            entity, _ = self._get_table_entity(table_uri=table_uri)
            guid_entity = self._driver.entity_guid(entity.entity['guid'])
            guid_entity.classifications(tag).delete()
        except Exception as ex:
            # FixMe (Verdan): Too broad exception. Please make it specific
            LOGGER.exception('For some reason this deletes the classification '
                             'but also always return exception. {}'.format(str(ex)))

    def put_column_description(self, *,
                               table_uri: str,
                               column_name: str,
                               description: str) -> None:
        """
        :param table_uri:
        :param column_name: Name of the column to update the description
        :param description: The description string
        :return: None, as it simply updates the description of a column
        """
        column_detail = self._get_column(
            table_uri=table_uri,
            column_name=column_name)
        col_guid = column_detail['guid']

        entity = self._driver.entity_guid(col_guid)
        entity.entity[self.ATTRS_KEY]['description'] = description
        entity.update(attribute='description')

    def get_column_description(self, *,
                               table_uri: str,
                               column_name: str) -> Union[str, None]:
        """
        :param table_uri:
        :param column_name:
        :return: The column description using the referredEntities
        information of a table entity
        """
        column_detail = self._get_column(
            table_uri=table_uri,
            column_name=column_name)
        return column_detail[self.ATTRS_KEY].get('description')

    @_CACHE.region('atlas_proxy', '_get_metadata_entities')
    def _get_metadata_entities(self, dsl_param: dict) -> List:
        try:
            # Fetch the metadata entities based on popularity score
            metadata_ids = self._get_flat_values_from_dsl(dsl_param=dsl_param)
            metadata_collection = self._driver.entity_bulk(guid=metadata_ids)

            metadata_entities: List = list()
            for _collection in metadata_collection:
                metadata_entities.extend(_collection.entities_with_relationships(attributes=["parentEntity"]))

            return metadata_entities

        except (KeyError, TypeError) as ex:
            LOGGER.exception(f'DSL Search query failed: {ex}')
            raise NotFoundException('Unable to fetch popular tables. '
                                    'Please check your configurations.')

    def get_popular_tables(self, *, num_entries: int) -> List[PopularTable]:
        """
        :param num_entries: Number of popular tables to fetch
        :return: A List of popular tables instances
        """
        popular_tables = list()
        query_metadata_ids = {'query': f'FROM Table SELECT metadata.__guid '
                                       f'ORDERBY popularityScore desc '
                                       f'LIMIT {num_entries}'}

        metadata_entities = self._get_metadata_entities(query_metadata_ids)

        for metadata in metadata_entities:
            table = metadata.relationshipAttributes.get("parentEntity")
            table_attrs = table.get(self.ATTRS_KEY)

            table_qn = parse_table_qualified_name(
                qualified_name=table_attrs.get(self.QN_KEY)
            )

            table_name = table_qn.get("table_name") or table_attrs.get('name')
            db_name = table_qn.get("db_name", '')
            db_cluster = table_qn.get("cluster_name", '')

            popular_table = PopularTable(
                database=table.get("typeName"),
                cluster=db_cluster,
                schema=db_name,
                name=table_name,
                description=table_attrs.get('description'))
            popular_tables.append(popular_table)

        return popular_tables

    def get_latest_updated_ts(self) -> int:
        pass

    def get_tags(self) -> List:
        """
        Fetch all the classification entity definitions from atlas  as this
        will be used to generate the autocomplete on the table detail page
        :return: A list of TagDetail Objects
        """
        tags = []
        for metrics in self._driver.admin_metrics:
            tag_stats = metrics.tag
            for tag, count in tag_stats["tagEntities"].items():
                tags.append(
                    TagDetail(
                        tag_name=tag,
                        tag_count=count
                    )
                )
        return tags

    def get_table_by_user_relation(self, *, user_email: str,
                                   relation_type: UserResourceRel) -> Dict[str, Any]:
        pass

    def get_frequently_used_tables(self, *, user_email: str) -> Dict[str, Any]:
        pass

    def add_table_relation_by_user(self, *,
                                   table_uri: str,
                                   user_email: str,
                                   relation_type: UserResourceRel) -> None:
        pass

    def delete_table_relation_by_user(self, *,
                                      table_uri: str,
                                      user_email: str,
                                      relation_type: UserResourceRel) -> None:
        pass
